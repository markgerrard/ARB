# Bus-Side Gate — Slice 3: probe re-landing convention + standing mutation gate over changed test files

**Change summary:** v5 — the CLOSE amendment (light path; CLOSED per owner go 2026-07-29, sol option 2). Applies exactly the five composed r4 prescriptions; everything else carries over from v4 verbatim. **CO-r4-P2-1 + SOL-R4-F1** (scoping/exemption class — third occurrence, escalated per the round rail; owner chose sol option 2): the no-legal-target closure is now precisely defined (§1.2/Task 3 — paired file + rootdir-path conftests + transitive static imports from scoped trees AND repo package roots, dotted names resolved against repo root + `src/` + `tools/*/` package dirs, so `src/`-rooted conftest imports like `tests/arb_memory/conftest.py:15-16` → `arb_memory.mcp.config` ARE visible legal candidates), exemption authority is **non-candidate-controlled** (an owner-reviewed, in-repo pinned allowlist read from the base ref — never a candidate-authored sidecar branch; L40 re-specified: the gate recomputes the closure and refuses any exemption whose recomputed closure holds a legal candidate), and **collection vitality** joins legal-target classification (a legal-target pair must collect ≥1 test at baseline; the four deselected implbench placeholders — collect zero, lead-verified, `pyproject.toml:89` — fall out automatically). The `tests/arb_memory/test_mcp_sdk_contract.py` example is worked: it HAS legal candidates under this rule and falls OUT of the no-legal-target population (§1.2, §10). **SOL-R4-F1 (population):** the v4 sweep numbers (413/49/46/3) are demoted to author-run estimate; the implementor recomputes the population with the corrected classifier at build time. **CO-r4-P2-2:** the `EXEMPT-AST` emission is rowed (new L41) and the JSON surface/env/duration fields + deterministic pair order are rowed (new L42), making §3's literalness claim literal (§4 and §7 now keyed to L1–L42). **CO-r4-P2-3:** L35/L36 retitled to the SIGTERM leg they actually prove; SIGINT unwinds by default, stated as prose (residual nil). Dispositions in new §14; §§11–13 and all sections r4 did not touch carry over from v4 verbatim. Citations new or changed in v5 verified against this repository at dev `016f1c02`; carried-over citations were verified at dev `235048f1` / `2f066e4f` per the v4 summary.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "a check must be able to fail" a pipeline property instead of a discipline —
*procedural until CI hosts it* (§1.1 honest limit; the hedge belongs here, not sixty lines down).
Every changed or new test file entering the integration branch must carry (or be covered by) a
declared mutation that provably makes it go RED — and RED **via the tests the declaration names**,
not via any ambient refusal — before it merges. Relanded exempt-lane probes are the first-class
citizens of this gate: the re-landing convention binds each probe to a mutation that reinstates the
remediated defect, so "the probe is green" becomes a checked claim that the fix is present.

**Architecture:** Three layers, smallest surface first. (1) `scripts/mutation_sweep.py` grows a
per-mutation `expect_failed` binding, exit-code-checked baseline-green and per-id skip-vacuity
refusals, an import-resolution anti-masking check, machine-readable results, restore-by-hash, and a
signal-handled restore boundary (§1.4) — it remains a standalone CLI. (2) A declaration convention:
each test file may carry a co-located sidecar `<test_file_stem>.mutations.json` declaring the
mutations that kill it and the test node ids expected to fail. (3) A standalone validator,
`scripts/changed_test_mutation_gate.py`, run **in place on the clean candidate checkout** by the
integrator: it computes the changed test files against an explicit `--base`, requires each in-scope
pair (test file + sidecar) to hold a valid declaration, runs the sweep per pair with the paired
file as the test command, and emits named checks plus a JSON summary. A survivor is a named FAIL
and exit 1 — never a warning. Tamper evidence is a sha256 sidecar whose pinned digest the
**invoker** verifies against the integration target's copy before running (§1.1). The gate itself
needs only git + pytest + the project venv; no DSN is required *by the gate* — the tests under
sweep need whatever they need, and that is stated honestly (§1.2, r1 CO-P1-4). That is what keeps
Slice 3 independent of Slice 2 and of 1e–1h, per the spec's independence claim (spec §10, lines
684–686).

**Tech Stack:** Python 3, pytest, stdlib (`ast`, `json`, `subprocess`, `hashlib`, `signal`), git.
Runner: the project venv interpreter (`.venv/bin/python`), per the repo's standing gate precedent
(`scripts/graph-sql-gate:8-12` — "pytest is NOT on PATH on this host"). No psycopg anywhere in
this slice's code.

**Spec:** `docs/superpowers/specs/2026-07-26-bus-side-gate-design.md` — binding sections: §6
(probe re-lands through the gated lane and "the standing mutation gate (§10, slice 3) gains a
genuinely behaviour-linked test to protect", lines 453–457), §9.5 ("mutation gate over changed
tests" in the close conjunction, lines 664–670), §10 row 3 (line 682) + the independence-and-
evidence paragraph (lines 684–686), §11 (testing-shape precedents). Also read, before writing any
gate test: `docs/defect-classes/refusal-is-ambient-assert-the-code.md` and
`docs/defect-classes/deny-proofs-need-adversarial-verification.md` — this gate exists because of
those two classes, and must itself survive both.

---

## 1. Decisions the named open questions demanded

### 1.1 Execution surface — RE-DECIDED (r1 CO-P0-1): a standalone validator run in place at integration; `run-gate` hosting rejected on its own containment mechanism

**What r1 established (verified here against the source).** v1 chose `scripts/run-gate` as host.
That host fingerprints the project before and after the gate runs and treats **any** difference as
a runner-level error: `before = dirt_snapshot(project)` at `gate_runner.py:632`, `after` at `:646`,
`dirt_clean = before.fingerprint == after.fingerprint` at `:665`, and `not dirt_clean ⇒
EXIT_GATE_ERROR (5)`, verdict `GATE-ERROR`, "gate mutated the project" at `:686-690`. The
fingerprint is deliberately maximal: `git status --porcelain=v1 -z --untracked-files=all
--ignored` (`gate_runner.py:268-276`), a content hash of every untracked path from `git ls-files
--others` with **no** `--exclude-standard` (`:172-192`), and a recursive content hash of the
git-dir/common-dir/hooks trees (`:195-218`, `:249-265`). The behaviour is pinned by tests:
`tests/test_gate_runner.py:275-287` (a project write → returncode 5) and `:290-305` (a write to a
**.gitignored** path → returncode 5). A mutation sweep writes the project by definition
(`mutation_sweep.py:125` writes mutated bytes; `:150` reverts via `git checkout --`; every pytest
run regenerates `__pycache__/` and `.pytest_cache/`, both gitignored — `.gitignore:2,6` — and both
still fingerprinted). Executed as v1 wrote it, every real run ends `GATE-ERROR` (exit 5), never
GREEN or RED. The host's design intent is explicit: "The validator works read-only"
(`docs/orchestrator-patterns.md:409`). v1 cited containment as a reason *for* the host while
specifying work that violates it — the contradiction cold-Opus named.

**The decision.** The three candidate surfaces, with trade-offs:

| Surface | What it gives | What kills or costs it |
|---|---|---|
| **(A) Standalone validator, in place on the clean candidate checkout — CHOSEN** | The domain-correct containment already exists in the sweep layer: dirty-tree refusal (`mutation_sweep.py:91-101`), restore-by-hash *detection* (Task 1, ported from `coverage_mutation_sweep.py:240-246` — the port detects a failed restore; *recovery* on this surface is the §1.4 boundary: signal-handled restore, the gate's `FAIL[tree]` abort (L28), and named operator commands), post-sweep clean assert (`mutation_sweep.py:160`). Imports resolve to the very checkout being mutated (the venv's editable install points at this repo's `src/` — no masking machinery needed for the merge-time run). Interpreter is the project venv python, which has pytest and all deps (precedent `scripts/graph-sql-gate:8-12`). This is how every mutation sweep in this repo actually runs today, including the standing `coverage_mutation_sweep` hook (`coverage_mutation_sweep.py:36-38`). | Loses run-gate's *structural* digest refusal, env stripping, and wall-timeout. Compensations: invoker-side digest check reading the pin from the **integration target ref** (below); the §1.4 recovery boundary; the gate's own JSON summary; fail-closed exit semantics. All honest-limit'd in §10 — and note the digest check under run-gate was equally procedural at the invocation layer, since nothing forces `run-gate` to be run either. |
| (B) Retain `run-gate`; sweep inside a throwaway local clone in TMPDIR | Keeps digest refusal (exit 3), protocol parsing, JSON summary; dirt check becomes a genuine control over the gate. | A chain of independently speculative machinery, each able to sink implementation: (i) under `run-gate` the gate executes as `uv run <gate>` from an empty temp cwd with a stripped env (`gate_runner.py:47`, `:639-645`) — sol demonstrated `ModuleNotFoundError: No module named 'pytest'` in exactly that shape (r1 SOL-F3), so the clone needs its own resolved environment; (ii) the project venv's **editable install** (`pyproject.toml:1-3` setuptools; `uv sync` installs the project editable) resolves `agent_redis_bridge` et al. to *this* repo's `src/`, not the clone's — mutations in the clone would be invisible unless a clone venv is built; (iii) building a clone venv means `uv sync --all-extras` per run with `uv.lock` **gitignored** (`.gitignore:9`), i.e. fresh resolution needing network — which run-gate's Linux network namespace blocks (`gate_runner.py:429-438`) in precisely the CI host that would someday run it; (iv) `_resource_limiter` imposes RLIMIT_AS 2 GiB / NOFILE 256 / CPU ≈ timeout+2 on every descendant (`gate_runner.py:444-466`), unmeasured against full-dep pytest. Rejected: too many unknowns stacked under an implementation plan. |
| (C) Extend `gate_runner` with a declared-mutation mode | One host for all gates. | **Scope implication stated plainly:** `src/agent_redis_bridge/gate_runner.py` is bridge code — a different ownership surface from repo tooling — and the dirt fingerprint is a *security control* other gates rely on (it is what detects hook-planting and git-internals tampering, `tests/test_gate_runner.py:308-332`). A mutation-allowed mode is a hole cut in a tested containment contract on behalf of one client. Rejected; if the owner ever wants it, it is its own reviewed bridge change, not a rider on this slice. |

Two v1 rejections stand unchanged: **re-enable CI now** (re-arms the entire pipeline — live
Postgres + Redis services, Playwright, node suites, `.github/workflows/ci.yml.disabled:18-46,
80-126`; CI config is protected, owner-executed; this plan ships a CI-ready snippet, Task 8) and
**pre-commit** (wrong diff, wrong latency budget, trivially bypassed via `--no-verify`).

**Tamper evidence on the chosen surface.** The gate keeps a digest sidecar
`scripts/changed_test_mutation_gate.py.sha256` in the run-gate record format
(`<timestamp> <digest>` per line, matching `gate_runner.py:124-127` for fleet consistency), with a
`--repin` mode that appends. The canonical invocation (Task 4) has the **invoker** verify the
digest *before* running, and read the expected pin from the **integration target ref**
(`git show "$BASE":scripts/changed_test_mutation_gate.py.sha256`), extracting the digest field with
`awk 'NF {pin=$NF} END {print pin}'` — this fixes r1 SOL-F2/CO-P1-1 (v1 passed the whole
timestamp-digest line to a 64-hex-only `--pin`, `gate_runner.py:151-154`) and closes a loophole v1
never named: a candidate branch that edits both the gate and its sidecar self-consistently would
pass any pin read from the candidate itself. Reading the pin from `$BASE` means gate-machinery
changes must land through review before they govern anyone else's merge. The one exception is the
landing that *introduces or changes* the gate machinery itself: for that merge the digest step is
against the candidate's own reviewed diff, and the manual text (Task 8) says so explicitly.

**Honest limit, stated in the §9.3 style:** until CI hosts it, both the digest check and the gate
invocation are procedural — REQUIRED manual steps with a good audit trail (the JSON summary + the
pinned digest cited at close), not unbypassable controls. The structural follow-up is the owner's
CI enable; the plan names that trigger and does not pretend otherwise. There is additionally **no
sandbox** on this surface: the gate runs with the invoker's environment and can, in principle, do
anything the invoker can. Its containment story is the sweep layer's own refusals plus the §1.4
recovery boundary plus review of the gate's code — stated at exactly that strength in §10.

### 1.2 Scope of "changed test files": explicit base flag, committed state, pair-aware exemption

- **Baseline: `--base <ref>` is an explicit, mandatory CLI flag** (the run-gate `--pass-env`
  plumbing is gone with the host). The gate resolves `git -C <repo> merge-base HEAD <base>`;
  a missing flag is an argparse usage error (exit 2); an unresolvable ref or failed merge-base is
  a named `FAIL[scope]`, never a silent empty diff (L1). **A base that resolves such that
  merge-base equals HEAD is also a named `FAIL[scope]`** — an empty diff produced by gating a
  branch against itself is indistinguishable from a wrong invocation, and grok executed exactly
  that vacuous-green shape (L2; folds GROK P2-4). At integration the orchestrator passes the
  integration target (`dev` today; `main` for release merges). Precedent for
  merge-base-against-target: `.github/workflows/ci.yml.disabled:54-59`.
- **The gate gates HEAD — the committed candidate state.** Changed files come from
  `git -C <repo> diff --name-status -M <merge-base>...HEAD` (rename-aware; commit-to-commit, no
  index involvement). Uncommitted work is not gated — integration merges commits — and the sweep's
  inherited dirty-tree refusal (`mutation_sweep.py:91-101`, no override by design `:15-17`)
  independently refuses to mutate a dirty checkout. AST-exemption base bytes come from
  `git show <merge-base>:<path>`; worktree bytes from HEAD's checkout.
- **Repo-root resolution (AGY-01, kept as discipline even though the temp-cwd hazard died with
  the host):** the gate takes `--repo` (default: cwd), resolves it, and **refuses (exit 2) unless
  `git -C <repo> rev-parse --show-toplevel` equals the resolved path** — no accidental subdir or
  wrong-checkout invocation (L31). Every git subprocess uses `git -C <repo>`; `run_sweep` receives
  the same resolved root. No git command in the gate ever relies on ambient cwd.
- **Pair scoping is change-driven and exemption is pair-aware (r1 SOL-F1).** A **pair** is (test
  file, sidecar). A pair is in scope when **either member** changed (added/modified/deleted/
  renamed). The AST exemption evaluates **only the test-file member** and can exempt the pair
  **only when the test file is the sole changed member**: any sidecar change — including deletion
  or weakening — forces the pair through declaration validation and the sweep regardless of the
  test file's AST (L5 sidecar-deleted ⇒ decl-missing; L6 sidecar-modified ⇒ revalidate + sweep).
  v1's ordering let a sidecar-only edit ride the unchanged test's AST equality into exemption —
  the bypassable-standing-gate defect sol demonstrated. Evaluation order is now normative:
  **scope → exemption (test-member-only) → declaration validation → sweep.**
- **One scoped-tree set; target refusal is per-pair, by import surface (r2 CO-P1-3 → r3
  CO-P1-1).** `SCOPED_TEST_TREES` — where changed **test files** are detected: `tests/**`,
  `tools/eval/tests/**`, `bench/implbench/tests/**`, `tools/authorbench/tests/**`,
  `tools/faba/tests/**`. The first four correspond to the CI suite steps
  (`.github/workflows/ci.yml.disabled:95-104`) — with one correction v2 got wrong: CI's
  `uv run pytest tools/authorbench -q` (`:104`) passes a pytest *rootdir argument*, not a test
  tree. The tree's tests all live under `tools/authorbench/tests/` (at the verification commit,
  every `test_*.py` under `tools/authorbench/` is in that subtree), while
  `tools/authorbench/authorbench/` is the tool's **production package** (`jail.py`, `bundle.py`,
  `judge.py`, …). The FABA suite is a standing baseline exercised outside CI, e.g.
  `docs/superpowers/plans/2026-07-27-bus-side-gate-slice1d-exempt-lane.md:94-96` — grok's r1
  citation audit confirmed this is a separate justification, not a false CI citation. A "test
  file" is a `.py` file under `SCOPED_TEST_TREES` matching pytest's `test_*.py` convention; the
  constant keeps its enumeration test (Task 3).
  v3's second constant — tree-shaped `REFUSED_TARGET_TREES` — is **gone: it recreated the
  un-passable-pair class one tree over (r3 CO-P1-1)**. Demonstrated member:
  `tests/defect_hunts/test_gate_assertions.py` imports only `ast` and `pathlib` (`:28-29`),
  everything its two tests depend on resolves under `tests/**` (its guard logic is in-module,
  `:36-83`; its subjects are the `GATE_TEST_FILES` bytes, `:36-40`), and Task 6 modifies it
  behaviourally — so under the tree rule it is in scope, not AST-exempt, and has **no compliant
  declaration**: this plan's own landing merge could not pass its own gate. The replacement rule
  refuses **what the paired run loads**, not a tree. A mutation target is refused iff it resolves
  to: (i) the paired test file itself; (ii) any `conftest.py` on the paired file's conftest chain
  (repo root down to its directory); (iii) any package `__init__.py` on the paired file's package
  path inside a scoped tree (these load on import — `tests/defect_hunts/__init__.py`,
  `tests/e2e/__init__.py` et al. exist at the verification commit); or (iv) any module in the
  paired file's **transitive static import closure** that resolves under a `SCOPED_TEST_TREES`
  root — closure computed by AST walk over `import`/`from … import` statements **and
  `pytest_plugins` declarations** (in the paired file and every chain conftest), traversing
  scoped-tree members only, never executing anything (Task 3). This preserves grok's anti-bypass
  property at its actual boundary: the sweep's test command is the paired file alone (§1.3), so
  only what that run *loads* can produce an ambient kill; a scoped-tree file the run does not
  load cannot manufacture one — mutating it either genuinely reds the named tests (a data
  subject, read as bytes) or survives and FAILs the declaration.
- **Both r3 stranded pairs have compliant declarations under the import-surface rule — worked
  here, pinned by L30.** (1) `tests/defect_hunts/test_gate_assertions.py`: refused surface =
  itself + `tests/conftest.py` + `tests/defect_hunts/__init__.py` (stdlib-only imports, no other
  chain conftest exists — the only conftests under `tests/` are `tests/conftest.py`,
  `tests/arb_memory/conftest.py`, `tests/e2e/conftest.py`). Its *subject* is the bytes of the
  three `GATE_TEST_FILES` (`:36-40`) — read via `pathlib`, never imported. A declaration
  targeting `tests/test_claim_gate.py` (inject a bare-refusal offender function) is **legal**
  and kills via `test_gate_tests_pin_the_mechanism_not_merely_the_refusal` — genuinely, on the
  guard's own mechanism; one targeting `tests/conftest.py` is `DECL-INVALID`. Task 6 step 3
  authors exactly this sidecar. (2) the authorbench pair: a declaration for `test_jail.py`
  targeting `tools/authorbench/authorbench/jail.py` (imported at `test_jail.py:10`, production,
  outside every scoped tree) is **legal**; one targeting `test_jail.py` itself,
  `tools/authorbench/tests/conftest.py`, or `tools/authorbench/tests/__init__.py` is
  `DECL-INVALID`. `test_jail.py` is unittest-based (`test_jail.py:4`), so its declared ids are
  class-form — the node-id form rule (Task 2, r3 CO-P2-3) is not hypothetical on this very
  example.
- **The stranded-pair sweep is an author-run estimate; the implementor recomputes the population
  with the corrected classifier at build time (SOL-R4-F1; demotion per the r4 close).** Method,
  recorded so the recomputation is reproducible: ripgrep/glob over the working tree at dev
  `2f066e4f`, all five scoped trees — 413 test files (`tests/**` 318, `bench/implbench/tests` 67,
  `tools/authorbench/tests` 11, `tools/faba/tests` 10, `tools/eval/tests` 7); a
  first-party-namespace scan (imports or path references to `src/` packages, `scripts/`,
  `arb_eval`, `authorbench`, implbench `harness`, `faba_*`) left 49 candidates with no static
  production reference; each was read and classified. **r4 falsified part of that classification
  (SOL-R4-F1, executed):** the four deselected `bench/implbench/tests` placeholders —
  `test_cell_acl_live.py`, `test_cell_runtime_live.py`, `test_process_ledger_live.py`,
  `test_sandbox_live.py` — were classified "production via conftest fixtures"; in fact each
  imports only `pytest` and carries an unconditional `pytest.fail`, their sole chain conftest
  (`bench/implbench/tests/conftest.py`) only inserts `bench/implbench` into `sys.path` and
  defines no fixture, and `pyproject.toml:89` (`addopts = "-m 'not live_bakeoff'"`) deselects
  them so they **collect zero tests** at baseline (lead-verified by execution: `no tests
  collected (1 deselected)` — SOL-R4-F1's paired-file evidence). The 49/46/3 split is therefore
  unreliable as stated and is carried only as an estimate — the gate is delta-scoped, so the
  population matters pair-by-pair at edit time, and the corrected classifier (collection
  vitality + the precise closure below) re-derives it mechanically at build time. Known
  corrections at authoring time: the four placeholders fall out of legal-target via collection
  vitality (below); `tests/arb_memory/test_mcp_sdk_contract.py` falls out of the no-legal-target
  population via the precise closure (worked below). `tests/e2e/test_spine.py` and
  `tests/e2e/test_hermetic.py` remain no-legal-target members under the precise closure — their
  chain conftests (`tests/conftest.py`, `tests/e2e/conftest.py`) carry no static repo imports
  and `tests/e2e/spine.py` is stdlib-only (r4 CO-P2-1's own evidence). `tests/e2e/test_runner.py`
  remains the named static-analysis limit case: its static closure is all-refused, but corpus
  data files and dynamically-loaded `skills/` modules (`tests/e2e/h2_harness.py` loads hunts via
  `importlib`) are legal targets, so it is classified not-stranded.
- **Handling — a non-candidate-controlled, owner-reviewed exemption allowlist, verified against
  a precisely defined closure (r4 CO-P2-1 + SOL-R4-F1; owner-selected sol option 2, matching the
  arc's retire-self-report direction), L40.** Definitions first, because r4 showed the v4 text
  supported two readings, each breaking one of v4's own claims (CO-r4-P2-1):
  - **Closure of a pair** = the paired test file + every `conftest.py` on its pytest rootdir
    path (repo root down to its directory) + every module **transitively** imported — via
    `import`/`from … import`/`pytest_plugins`, statically, never executing anything — from those
    files, from `SCOPED_TEST_TREES` members reached by the walk, and from repo package roots,
    with dotted names resolved against the **enumerated resolution roots: repo root, `src/`, and
    the tool package dirs (`tools/*/` package roots, e.g. `tools/authorbench/`)**. `src/`-rooted
    imports ARE therefore visible: `tests/arb_memory/conftest.py:15-16` (`from
    arb_memory.mcp.config import mcp_role_name` / `from arb_memory.mcp.grants import
    apply_mcp_grants`) resolves to `src/arb_memory/mcp/config.py` / `grants.py`, which exist at
    the verification commit. Dotted names resolving to no repo file (stdlib, site-packages) are
    ignored — not repo files, not mutation targets.
  - **A legal target** = a closure member that is (i) not on the pair's refused surface (above)
    and (ii) not itself scoped-tree test/support (a test file, conftest, or package
    `__init__.py` under a scoped tree). **Collection vitality is part of legal-target
    classification (SOL-R4-F1):** a pair classified legal-target must **collect ≥1 test at
    baseline** under the gate's own test command — a pair collecting zero (the four deselected
    implbench placeholders, `pyproject.toml:89`) can demonstrate fail-ability under no
    declaration whatever its closure holds, so it falls out of legal-target automatically (if
    swept it reaches the sweep's zero-collected refusal, `mutation_sweep.py:136-142`).
  - **Exemption authority is not candidate-authored (sol option 2).** The v4 `no_legal_target`
    sidecar branch is **gone**: a sidecar carrying a `no_legal_target` key is `DECL-INVALID`
    outright (L40 candidate-authored leg). In its place, an owner-reviewed, in-repo pinned
    allowlist file — `scripts/mutation_gate_exemptions.json`, one entry per exempt pair: path +
    `reason` (`harness-self-test` | `environment-contract` | `deselected-placeholder`) + a
    one-line `subject` — like every other pin: the gate reads it from the **base ref**
    (`git show <base>:scripts/mutation_gate_exemptions.json`; absent at the base ⇒ empty
    allowlist), so a candidate branch cannot self-exempt — entries govern merges only after
    landing through owner-reviewed merges to the target, the same authority shape as the §1.1
    digest pin, with the same first-landing exception. The allowlist lands **empty** in this
    slice: no current no-legal-target member is touched by this slice's diff, and the first
    behavioural edit to one forces the owner entry through review.
  - **L40 re-specified (both ways + probe leg):** for an in-scope pair with an allowlist entry,
    the gate **recomputes the closure** and refuses the exemption if the recomputed closure
    contains any legal candidate — `FAIL[pair:<path>]: DECL-INVALID — allowlisted
    no-legal-target, but legal candidates exist: <paths>`; accepted ⇒ loud
    `PASS[pair:<path>]: EXEMPT-NO-LEGAL-TARGET (<reason>) — <subject>`, recorded in the JSON
    summary. A pair carrying `PROBE_PROVENANCE` may never be exempted this way — probes
    reinstate production defects by construction (L40 probe leg).
  - **The worked example (r4 CO-P2-1's decisive shape): `tests/arb_memory/test_mcp_sdk_contract.py`
    HAS legal candidates under this rule and is NOT a no-legal-target member.** Its own imports
    are `importlib.metadata`, `inspect`, and the installed `mcp` SDK only
    (`test_mcp_sdk_contract.py:1-12`) — but its closure includes its rootdir-path conftests, and
    `tests/arb_memory/conftest.py:15-16` imports `arb_memory.mcp.config` and
    `arb_memory.mcp.grants`, which resolve under `src/` — outside every scoped tree, off the
    refused surface ⇒ **legal candidates**. An allowlist entry for this pair is therefore
    refused by the recompute, naming those candidates — and this exact real-repo shape (chain
    conftest importing a production module) is L40's reject-leg fixture (r4 CO-P2-1
    alternative 3). What those candidates buy this pair is bounded and stated honestly in §10:
    a mutation to `arb_memory.mcp.*` can red this pair only by breaking the conftest import — a
    collection error the sweep *refuses* (count-drift, `mutation_sweep.py:151-156`), never a
    kill — so a future behavioural edit to this file FAILs closed at the gate and forces an
    owner decision through review. Fail-closed-and-loud is the accepted price of
    non-candidate-controlled exemption; nothing fires at landing (the file is untouched by this
    slice). The four implbench placeholders take the sibling route: collection vitality keeps
    them out of legal-target, so the first edit to one forces the owner to choose between a
    `deselected-placeholder` allowlist entry and the `SCOPED_TEST_TREES` ratchet question
    SOL-R4-F1 posed — an owner disposition either way, never a silent pass.
- **Honest DSN posture (r1 CO-P1-4).** v1's "no DSN" claim was false for `tests/arb_memory/**`,
  which skips without `ARB_MEMORY_DSN` (`tests/arb_memory/conftest.py:83-87, 109-113`;
  CI provisions live services and exports the DSNs, `.github/workflows/ci.yml.disabled:83-89`).
  Corrected posture: **the gate itself** needs only git + pytest + the project venv; **the tests
  under sweep** need whatever they need, and the gate neither provisions nor fakes it. Those DSN
  skips are **fixture-level** (`pytest.skip` inside the `scratch` and `empty_schema_conn`
  fixtures), so a pair can be *partially* skipped — which is why the vacuity control is per-id
  (Task 1(b), L29), not merely per-file: a pair whose baseline passes zero tests refuses with the
  file-level reason (L25), and a pair whose baseline skips *a declared node id* refuses with
  `NAMED-TEST-SKIPPED` (r2 CO-P1-2; reason split r3 CO-P2-3) — never a false `WRONG-MECHANISM`
  blaming the author. This is `graph-sql-gate`'s refuse-to-skip stance
  (`scripts/graph-sql-gate:3-5,15-18`), applied per declared id. The manual text (Task 8) tells
  the integrator: export `ARB_MEMORY_DSN` when gating pairs under `tests/arb_memory/`, or expect
  a named FAIL saying the gate cannot demonstrate fail-ability in an environment where the tests
  do not run. The JSON summary records DSN presence/absence as an environment fact.
- **Worktree dispatch suites are covered at integration, not specially.** When a worker's branch
  is gated pre-merge, their test files *are* the changed files. Workers may additionally run the
  gate inside their worktree with `--base <branch point>` (brief line, Task 8), and for that case
  the sweep's **import-resolution anti-masking check** (Task 1, L26) matters: worktree venvs on
  this fleet are CoW mirrors whose editable installs can resolve imports to the *parent* checkout,
  and a sweep that mutates worktree bytes while pytest imports parent bytes reports garbage. The
  control is ported from `coverage_mutation_sweep.py` (PYTHONPATH forced to the target root's
  `src`, and every run asserts the imported module's `__file__` resolves under the target root —
  docstring `:32-34`, resolution check `:152-163`): mismatch ⇒ `SweepRefused`, named. The
  merge-time run on the primary checkout is the authoritative one either way.
- **Out of pattern:** the node suite (`tools/pi-sdk-host`) — pytest-only gate, named in Out of
  scope. Deleted test files are exempt (nothing left to prove RED; deleting tests is the panel's
  business), and the exemption is emitted visibly — `PASS[pair:<path>]: EXEMPT-DELETED` — with
  its own ledger row (L38, r3 CO-P2-2). Renamed files are treated as changed at the new path,
  eligible for AST exemption against the old path via `git show <merge-base>:<old-path>` (L9
  pins that a rename cannot silently scope out) — **and the declaration must move with the
  rename**: if the old stem had a sidecar at the merge-base and no sidecar exists at the new
  stem in HEAD, the pair is `FAIL[pair:<new path>]: DECL-MISSING`, AST equality notwithstanding.
  v2 let a pure rename ride `EXEMPT-AST` into a merge that left the renamed file undeclared and
  an orphan sidecar behind (r2 cold-Opus P2-6; L34). Comment/whitespace-only edits are
  mechanically exempt via AST equality (`ast.dump` comparison) — an *automatic* exemption,
  deliberately not a declarable one, so there is no free-text bypass field for honest
  misclassification to pool in (spec §8). Any parse failure on either version ⇒ not exempt,
  fail-closed (L8).

### 1.3 Runtime budget: delta-scoped, paired-file test command, deterministic ceiling — no sampling

- **Targeting rule:** the gate runs **only** the mutations declared for **in-scope, non-exempt**
  pairs, and each mutation's test command is **the paired test file alone**
  (`[<venv python>, "-m", "pytest", <paired_file>, "-q", "-rA", "--no-header"]`), not the full
  suite. **The paired file is passed as a repo-relative path** — the sweep runs commands with
  `cwd=repo` (`mutation_sweep.py:133`) and pytest echoes node ids *as the path was given on the
  command line*, so an absolute path would make every declared-id comparison miss and report a
  false `WRONG-MECHANISM` for every pair (r2 cold-Opus P2-8; precedent: the standing sweep passes
  a relative path, `coverage_mutation_sweep.py:174`). The same exactness applies to the id's
  tail: parametrised ids (`::test_x[case]`) and unittest class ids (`::Class::test_x`) must be
  declared in exact collected form (Task 2; L29's `NAMED-TEST-NOT-COLLECTED` refusal names the
  collected near-misses instead of blaming the environment — r3 CO-P2-3). L22's fixture asserts
  at least one real declared-id match against a parsed failed id, so a path-form regression
  reds. `-rA` makes the short summary name every outcome per node id, which the per-id baseline
  control parses (Task 1(b)). The interpreter is the same one running the gate — **correct by
  the canonical invocation and fail-closed otherwise**: nothing in the gate constructs the
  interpreter, the canonical block (Task 4) launches it with `.venv/bin/python`, and a wrong
  interpreter has no pytest, so the baseline collects nothing and the sweep refuses (r2
  cold-Opus P2-2; the r1 SOL-F3/CO-P1-7 hazard was a property of the `uv run`-from-temp-cwd
  host, not of the command). The gate imports `run_sweep` directly and passes the pre-split
  `list[str]` (`mutation_sweep.py:128` signature) — no CLI string re-parsing (folds AGY-S3P-03).
  Cost per mutation is one single-file pytest run. Scale evidence that per-change inject-revert
  is tractable: the standing coverage sweep runs a 55-mutation floor routinely
  (`coverage_mutation_sweep.py:65`, `MIN_MUTATION_FLOOR = 55`) as a standing hook
  (`coverage_mutation_sweep.py:36-38`).
- **Ceiling:** `MAX_MUTATIONS_PER_RUN = 64` (constant, tested). A run whose declared in-scope
  mutation count exceeds the ceiling emits `FAIL[budget]` telling the author to split the change —
  and **no sweep runs at all** (a partial sweep would be a sample); the no-partial-sweep property
  has its own ledger row (L17), not just the FAIL string (closes r1 CO-P1-6 item 3). Wall-clock:
  the gate has no hosting timeout any more; it records per-pair durations in the JSON summary,
  and the invoker may wrap it in `timeout --signal=INT <secs>` — the §1.4 handlers turn INT/TERM
  into an unwinding exit that restores the tree, and the run exits non-zero, fail-closed for
  merging. A bare `--signal=KILL` (or an expiring `--kill-after`) cannot be caught and falls to
  §1.4's hard-kill posture. First real measurements are a Task 4/Task 7 deliverable (Open
  question 3).
- **Sampling: none, and rejected by design.** A sampled merge gate converts "survivor ⇒ named
  FAIL" into "survivor ⇒ probably a FAIL", which violates this plan's second constraint outright.
  Random-operator mutation tools (mutmut-class) are rejected for the same reason from the other
  side: equivalent mutants produce permanent false FAILs, and the only way to live with them is to
  demote survivors to warnings — again the constraint, violated. Declared mutations make every
  survivor actionable by construction. Probabilistic verification has a designed home and it is
  not here: Slice 2's randomised spot-check (spec §10 row 2, line 681).

### 1.4 Recovery boundary on the in-place surface (r2 SOL-F3 + AGY-R2-01 + CO-P1-1, triple-convergent)

**The defect v2 left open.** The chosen surface mutates the real integration checkout. v2's ported
restore control *detects* a failed restore and raises — it does not restore
(`coverage_mutation_sweep.py:240-246`: `git checkout --`, hash-compare, `raise
SweepRefused("RESTORE FAILED …")`); the sweep's only restore is the `git checkout --` inside the
per-mutation `finally` (`mutation_sweep.py:145-150`); and a signal that terminates without
unwinding skips that `finally` entirely (CPython's default SIGTERM disposition terminates the
process without stack unwinding). Three uncovered states followed, all specific to running in
place:

1. **Restore-hash mismatch at pair k.** Mutated bytes stay on disk; the gate emits one FAIL for
   pair k and continues; pairs k+1…n then all refuse with the sweep's *dirty-tree* reason
   (`mutation_sweep.py:91-101` — "Commit or stash first. There is no override.") — a string that
   blames the integrator's hygiene for damage the gate itself did. That is the
   misattributed-reason class this plan exists to kill
   (`docs/defect-classes/refusal-is-ambient-assert-the-code.md`), reappearing on the new surface.
   And if pair k is the *last* pair, the post-sweep clean assert (`mutation_sweep.py:160`) is
   never reached (the raise precedes it) and no check ever states the tree is dirty.
2. **A killed run.** SIGTERM without a handler terminates without running the `finally`
   (`mutation_sweep.py:149-150`); the verdict is fail-closed for merging, but the tree is not.
3. **Any uncaught exception between `apply_mutation` and the restore** — same shape.

Mutated production bytes sitting in the integration checkout are a defect-injection path
(`git commit -a`, a later `git stash pop`, a worker told "just clean the tree").

**The boundary — three mechanisms, each with a ledger row (the signal mechanism with one row PER
HANDLER):**

1. **Per-mutation try/finally with hash-verified restore (Task 1; L23).** The restore stays inside
   the per-mutation `finally` (where `mutation_sweep.py:149-150` has it today) and gains the hash
   verification, so every *ordinary* exception path — including a `SweepRefused` raised after the
   test run — restores before propagating.
2. **Signal handling in the CLI entry points — an exhaustive enumeration (r3 SOL-R3-F1 /
   CO-P2-1).** This plan mandates signal handlers in **exactly two** entry points, and this
   enumeration is exhaustive — no other executable entry point exists among this plan's
   deliverables (the two scripts below are its only files with a `main()`; the gate's `--repin`
   mode runs inside the same gate `main()`):
   - `scripts/mutation_sweep.py::main()` — deny-proof **L35** (sweep-CLI SIGTERM leg, Task 1);
   - `scripts/changed_test_mutation_gate.py::main()` — deny-proof **L36** (gate-CLI SIGTERM leg,
     Task 4).
   Each registers SIGINT/SIGTERM handlers that raise, so the per-mutation `finally` runs and the
   tree is restored before exit. Registration is in `main()`, **not** module import, since tests
   and the gate import `run_sweep` as a library. The two handlers are **independent controls**:
   the gate never execs the sweep CLI (it imports `run_sweep`), so the sweep's handler is not in
   the gate's process — deleting only the gate's handler restores CPython's default SIGTERM
   termination for every real integration run while L35 stays green. That is why each handler
   has its own row and why Task 6 inject-reverts them independently. Required because CPython's
   default SIGTERM disposition terminates without unwinding; without the handler, `timeout(1)`'s
   default TERM strands the tree mutated. **The deny-proofs exercise the TERM leg only, and the
   rows say so (r4 CO-P2-3):** CPython's default SIGINT disposition already raises
   `KeyboardInterrupt`, so the per-mutation `finally` unwinds with or without the handler and the
   SIGINT registration shapes only the exit path — behavioural residual nil, stated here as
   prose rather than over-claimed by a row title. SIGTERM is the load-bearing leg, and it is the
   leg L35/L36 prove.
3. **Gate-level dirty-after-sweep check with abort, tracked and untracked residue distinguished
   (Task 4; L28; r3 CO-P2-4).** After **every** pair — PASS, FAIL, refusal, or an exception
   escaping `run_sweep` — the gate runs `git -C <repo> status --porcelain` and **parses the
   status codes**. Tracked modifications (` M`, `MM`, ` D`, …) ⇒
   `FAIL[tree]: DIRTY-AFTER-SWEEP — <paths> — run: git -C <repo> checkout -- <paths>; confirm
   clean; then re-run the gate`. Untracked residue (`??`) ⇒ a **separate** reason,
   `FAIL[tree]: UNTRACKED-RESIDUE — <paths> — likely left by the gated test run, not by the
   sweep's restore; preview removal with: git -C <repo> clean -n -- <paths>; remove after the
   preview matches with: git -C <repo> clean -f -- <paths>` — because `git checkout --` on an
   untracked path errors with "pathspec … did not match any file(s) known to git" and cleans
   nothing, and the repo has precedent for test-generated untracked residue
   (`.gitignore:64-67`, the SQLite WAL sidecars). Mixed residue emits one `FAIL[tree]` per class
   present. Either reason **aborts the remaining pairs**: no later pair is allowed to emit a
   dirty-tree refusal that misattributes the cause, and the last-pair blind spot is closed
   because the check also runs after the final pair.

**Operator recovery (this exact text also lands in the Task 8 manual subsection):**

```bash
git -C <repo> status --porcelain        # enumerate the damage
git -C <repo> checkout -- <paths>       # restore each listed TRACKED path (' M', ' D', …)
git -C <repo> clean -n -- <paths>       # preview removal of UNTRACKED residue ('??' paths)
git -C <repo> clean -f -- <paths>       # remove it once the preview matches expectations
git -C <repo> status --porcelain        # MUST now print nothing — confirm before anything else
```

Never `git commit -a`, `git stash`, or merge from the checkout while it is in this state; re-run
the gate after confirming clean.

**What this can and cannot guarantee — stated at handler-coverage strength (r3 CO-P2-6).** Every
failure path the §1.4 mechanisms cover either restores the tree or reports the damage loudly in
the same run — a claim scoped to what the handlers and the `finally` cover, not to every
catchable signal: a second SIGTERM landing while the `finally`'s `git checkout --` is itself
running, or one landing between `run_sweep`'s return and the gate's tree check, is catchable in
principle but not covered, and falls to next-touch detection like the hard kills. **SIGKILL,
OOM-kill, and power loss cannot be caught** — no in-process design changes that — and can leave
mutated tracked bytes on disk with no FAIL emitted. The failure is loud on the *next* touch (the
sweep's own dirty-tree refusal, `mutation_sweep.py:91-101`, fires before any future sweep runs),
and recovery is the same commands above. The residual — an integrator who merges from a
hard-killed checkout without re-running the gate — is procedural, the same class as §1.1's
invocation limit (§10).

## 2. Relationship to existing tooling — build on, not supersede

- **`scripts/mutation_sweep.py` is built on (extended in place, CLI-compatible).** It already
  encodes, as refusals, four incident-derived invariants this gate inherits rather than re-learns:
  dirty-tree refusal (`mutation_sweep.py:91-101`), zero-tests-collected refusal (`:136-142`),
  per-mutation collected-count equality (`:151-156`), and anchor-exactly-once /
  must-actually-mutate (`:104-124`). Its docstring records the measured incidents behind the first
  two (`:10-24`). What it lacks for gate duty, all closed in Task 1: (a) it treats *any* test
  failure as a kill (`survived: not res.failed`, `:158`) — the bare-refusal defect in
  `refusal-is-ambient-assert-the-code.md` terms; (b) it never inspects `baseline.failed` **or the
  pytest process exit code** (`:132-134` discards `returncode` and keeps only parsed output), so a
  pre-red baseline — or an internal-error run whose parse looks green — can launder kills
  (GROK-P1-2; r2 SOL-F2); (c) it counts `skipped` toward `collected` (`:72-75`), so an
  all-skipped pair sails past both inherited refusals and would be recorded a survivor with a
  *false* reason string — and a *partially*-skipped pair passes even a per-file floor, which is
  why the control is per-id (r1 CO-P1-4; r2 CO-P1-2); (d) its revert is a bare `git checkout --`
  (`:150`) with no hash verification and no signal boundary (§1.4); (e) nothing asserts that the
  interpreter actually imports the code being mutated (the CoW-worktree masking case, §1.2).
- **`scripts/coverage_mutation_sweep.py` is not superseded and not touched — but three of its
  controls are ported:** restore-by-hash detection (`coverage_mutation_sweep.py:240-246`), the
  green-control ("prove the harness can see success before believing its refusals" — `unit_green`
  `:166-179`, used as a control at `:508-518`), and the import-path assertion (`:32-34`,
  `:152-163`). It remains a domain-specific *exhaustive* catalogue for `claim_resolver`'s
  `PRIVILEGE_COVERAGE` with its own floor, live-PG legs, and standing hook. Exhaustive-catalogue
  and delta-scoped-declaration are different instruments answering different questions; both
  remain.

## 3. Global constraints

- **A survivor is a named FAIL, never a warning** (enforced in Task 4, deny-proved in Task 6).
  Gate exit is 1.
- **Red-for-the-stated-reason, with causality:** every declared mutation carries a non-empty
  `expect_failed` list of test node ids inside the paired file; the kill rule is
  **`expect_failed ⊆ (mutated.failed − baseline.failed)`** with the baseline required green —
  where green now means: **pytest exit code 0** (the explicitly justified singleton — exit 1 is
  test failures, 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected: none of
  those is the comparison the report claims, and an internal-error exit with a green-looking
  parse must refuse, r2 SOL-F2), zero parsed failures, **and every named `expect_failed` id
  collected and passed at baseline** (r2 CO-P1-2; L24, L29). A mutation that reds the run without
  newly failing every named id is a wrong-mechanism FAIL, not a kill
  (`docs/defect-classes/refusal-is-ambient-assert-the-code.md`: "Red is not the assertion.
  Red-for-the-stated-reason is."; causality control per GROK-P1-2).
- **Mutations may not target the paired run's refused import surface (§1.2)** — the paired test
  file itself, any `conftest.py` on its chain, any package `__init__.py` on its package path
  inside a scoped tree, or any scoped-tree module in its transitive static import closure
  (`pytest_plugins` declarations included). v1's rule left `conftest.py`, fixture and harness
  modules as legal kill targets, and grok demonstrated the bypass: break a fixture in
  `tests/conftest.py`, and every named id fails via ambient collection error while production
  code goes untouched. v3's tree-shaped widening over-corrected into un-passable pairs (r3
  CO-P1-1). The enforced rule is: a declaration whose `file` resolves onto the pair's computed
  refused surface is `FAIL[pair]: DECL-INVALID`, with `tests/conftest.py` as the canonical
  refused fixture (L13); a legal target *outside* the surface — the authorbench production
  package beside its tests, an unimported sibling test file beside a meta-test — is pinned legal
  by L30, so an over-broad implementation reds rather than staying green; and the
  owner-allowlisted no-legal-target exemption is verified against the recomputed closure, both
  ways, by L40 (§1.2 — never candidate-authored, r4 sol option 2). This prose states exactly the
  enforced rule, no more (the r1 prose-stronger-than-mechanism defect, GROK-P1-1 / r1 cold-Opus
  P2-1).
- **No silent scope:** base explicit and mandatory; base-resolves-to-HEAD refused; the scope check
  always states the resolved base sha, head sha, and the counts examined, so a wrong-base run is
  readable in the summary — and that always-stated property has its own ledger row (L37, r3
  CO-P2-2), since §10's wrong-but-resolvable-base residual leans on it.
- **Fail-closed:** any `SweepRefused` (dirty tree, zero collected, pre-red or nonzero-exit
  baseline, named-id-not-collected or named-id-skipped at baseline, all-skipped baseline, count
  drift, restore-hash mismatch, import-resolution mismatch, anchor errors) maps to a named FAIL
  for the affected pair with the sweep's own reason string; the gate never converts a refusal
  into a pass, a skip, or a misattributed survivor. And when a refusal or crash leaves the
  checkout dirty, the gate's own `FAIL[tree]` (§1.4, L28) reports it with the reason matching
  the residue class — `DIRTY-AFTER-SWEEP` for tracked damage, `UNTRACKED-RESIDUE` for test-run
  leftovers — and **aborts the remaining pairs**: later pairs are never allowed to refuse with a
  dirty-tree reason that blames the integrator for damage the gate did.
- **Protocol, restated for the chosen surface (r1 CO-P1-2):** with `run-gate` gone, its
  `parse_checks` 1:1 header/result contract and check-id charset (`gate_runner.py:37-44,
  308-361`) no longer bind this gate. The gate defines its own protocol: it prints one
  `PASS[<check-id>]: <message>` or `FAIL[<check-id>]: <message>` line per check — ids are `scope`,
  `budget`, `sweep-pin`, `tree`, and `pair:<repo-relative-path>` (raw paths; no lossy `/`→`.`
  encoding, so r1 cold-Opus P2-7's collision class does not exist here) — with the *reason class*
  (`DECL-MISSING`, `DECL-INVALID`, `SURVIVOR`, `WRONG-MECHANISM`, `SWEEP-REFUSED`,
  `DIRTY-AFTER-SWEEP`, `UNTRACKED-RESIDUE`) as the first token of the FAIL message, never encoded
  into the id; visible exemptions (`EXEMPT-AST`, `EXEMPT-DELETED`, `EXEMPT-NO-LEGAL-TARGET`) are
  the first token of the corresponding PASS message. Exit contract: **0 iff every emitted check
  is PASS; 1 if any FAIL; 2 for usage/precondition refusals** (mirroring `mutation_sweep.py:34`).
  The JSON summary (`--json`) carries every check verbatim plus scope facts, environment facts,
  per-pair sweep results (including each pair's computed refused surface), durations, and the
  tree-check outcome.
- **Every mechanism this plan mandates is deny-provable, and §4 is the complete enumeration:**
  the ledger names, per mechanism, the test that goes RED when the mechanism is deleted —
  including the rows r1 found missing (binding-none refusal L14, parse-failure fail-closed L8,
  no-partial-sweep L17, rename scoping L9, guard extension L27; closes r1 CO-P1-6), the rows r2
  found missing or newly mandated (recovery boundary L28/L35, per-id baseline vitality L29,
  boundary pinning L30, repo-root refusal L31, `kind` validity L32, reinstate-presence L33,
  rename-sidecar-move L34; closes r2 cold-Opus P2-1's three gaps), the rows r3 found missing
  or newly mandated (gate-CLI signal handler L36, always-stated scope facts L37, deleted-file
  exemption L38, `--repin` record format L39 — closes r3 cold-Opus P2-2 — plus the
  import-surface/no-legal-target row L40 and the rewritten L13/L30, r3 CO-P1-1), and the rows
  r4 found missing (visible `EXEMPT-AST` emission L41; JSON surface/environment/duration field
  presence + deterministic pair order L42 — closes r4 CO-P2-2). §7's Class-V exit key covers all
  of L1–L42. Task 6 demonstrates each row by inject-revert before the plan's review loop may
  close (deny-proofs are themselves adversarially verified,
  `docs/defect-classes/deny-proofs-need-adversarial-verification.md`). Mechanisms this plan
  relies on but does not own are cited, not re-proved, and listed under the ledger.
- **Cross-slice claims cited, not asserted:** every claim in this plan about
  `gate_runner`/`mutation_sweep`/CI/seat-preflight behaviour carries a file:line citation verified
  at dev `235048f1` (carried over), dev `2f066e4f` (new in v4), or dev `016f1c02` (new in v5);
  the seat-preflight trace in Task 7 is cited to the r4 cold-Opus record that owns it, then
  settled *by execution* there.
- **No fleet claims:** this plan claims gate behaviour only where demonstrated — fixture repos in
  the test suite plus this repository at the verification commit, n-of-n. Nothing here claims CI
  enforcement (CI is disabled), and nothing claims any seat-host property.

## 4. Deny-proof ledger (mechanism → named RED test)

Plan-mandated controls, each with the test that goes RED when the mechanism is deleted or
neutered. Task 6 demonstrates every row by inject-revert. Rows L1–L21, L28, L30–L34, and L36–L42
live in `tests/test_changed_test_mutation_gate.py`; L22–L26, L29, and L35 in
`tests/test_mutation_sweep_expect_failed.py`; L27 in
`tests/defect_hunts/test_gate_assertions.py`.

| # | Mechanism | Delete/neuter it and this test REDs |
|---|---|---|
| L1 | Mandatory, resolvable `--base` | `test_missing_or_unresolvable_base_is_named_fail_not_empty_diff` |
| L2 | Base-resolves-to-HEAD refusal | `test_base_resolving_to_head_is_named_scope_fail` |
| L3 | Changed-file detection itself (control: the gate can *see* a change) | `test_gate_sees_a_changed_test_file_in_fixture_repo` |
| L4 | Declaration requirement for in-scope pairs | `test_changed_test_without_declaration_fails_by_name` |
| L5 | Sidecar-deletion coverage (pair in scope when only the sidecar was deleted) | `test_deleting_sidecar_alone_is_decl_missing_fail` |
| L6 | Sidecar-modification coverage (no AST-exemption bypass for sidecar-only edits) | `test_modifying_sidecar_alone_enters_validation_and_sweep` |
| L7 | AST-exemption tightness (only truly AST-identical is exempt) | `test_behavioural_edit_is_not_ast_exempt` |
| L8 | AST-exemption fail-closed on parse failure | `test_unparseable_version_is_not_exempt` |
| L9 | Rename scoping (a rename cannot scope out) | `test_renamed_test_file_stays_in_scope` |
| L10 | Survivor ⇒ named FAIL + exit 1 | `test_survivor_is_named_fail_and_exit_red` |
| L11 | `expect_failed` binding (wrong-mechanism detection at the gate) | `test_red_via_unnamed_test_is_wrong_mechanism_fail` |
| L12 | Non-empty `expect_failed` / ids-inside-paired-file validation | `test_empty_or_foreign_expect_failed_is_decl_invalid` |
| L13 | Import-surface self-mutation refusal (paired file; conftest chain; package `__init__`; imported scoped-tree modules — transitive, `pytest_plugins` included) | `test_mutation_targeting_paired_file_is_decl_invalid` **and** `test_mutation_targeting_conftest_chain_or_imported_scoped_module_is_decl_invalid` (parametrised: chain conftest / package `__init__` / direct import / transitive import / `pytest_plugins` module) |
| L14 | Gate refusal of `binding: none` (old-format) declarations | `test_unbound_declaration_is_decl_invalid_at_gate` |
| L15 | Sweep-refusal propagation (reason surfaces verbatim) | `test_zero_collection_refusal_is_named_fail_with_reason` |
| L16 | Budget ceiling | `test_over_ceiling_is_fail_budget` |
| L17 | No-partial-sweep on budget breach | `test_over_ceiling_runs_no_sweep_at_all` |
| L18 | Probe-provenance hook (marker without declaration; marker shape) | `test_probe_provenance_file_requires_declaration`, `test_probe_marker_shape_invalid_is_named_fail` |
| L19 | Transitive sweep pin (`mutation_sweep.py` digest vs embedded constant) | `test_sweep_source_drift_is_named_refusal` |
| L20 | Gate exit contract (0 iff all PASS; 1 any FAIL; 2 refusal) | `test_exit_code_contract` |
| L21 | JSON summary fidelity (summary checks == emitted checks) | `test_json_summary_matches_emitted_checks` |
| L22 | Kill rule: named-failure superset over *new* failures | `test_missing_named_failure_is_not_a_kill` |
| L23 | Restore-by-hash detection | `test_restore_hash_mismatch_refuses` |
| L24 | Baseline-green causality control (parsed failures **and** exit code) | `test_prered_baseline_refuses` **and** `test_green_parse_nonzero_exit_baseline_refuses` |
| L25 | File-level skip-vacuity refusal (baseline passed == 0) | `test_all_skipped_baseline_refuses` |
| L26 | Import-resolution anti-masking | `test_masked_import_resolution_refuses` |
| L27 | Bare-refusal guard catches the gate-exit assertion shape | extended `test_the_guard_itself_is_not_vacuous` (synthetic offender: `assert result.returncode == 1` with no reason-string assert) |
| L28 | Gate-level residue check + abort, tracked vs untracked reasons (no false-reason cascade) | `test_failed_restore_yields_distinct_tree_fail_and_aborts_remaining_pairs` (fixture simulates a failed restore) **and** `test_untracked_residue_is_distinct_tree_fail_with_clean_guidance` (fixture pair plants an untracked file; asserts `UNTRACKED-RESIDUE`, the `git clean -n` guidance, and the abort) |
| L29 | Per-id baseline vitality, reasons split (id form vs environment) | `test_named_id_not_collected_at_baseline_refuses_naming_id_form` **and** `test_named_id_skipped_at_baseline_refuses_naming_environment` |
| L30 | Import-surface boundary (legal target outside the surface stays legal — both worked examples) | `test_authorbench_production_target_is_legal_while_its_paired_surface_is_refused` **and** `test_unimported_scoped_tree_file_is_legal_target_for_meta_test_pair` |
| L31 | Repo-root toplevel-equality refusal (exit 2) | `test_repo_not_git_toplevel_is_usage_refusal` |
| L32 | Declaration `kind` present-and-valid | `test_missing_or_invalid_kind_is_decl_invalid` |
| L33 | Probe sidecar requires ≥1 `reinstate` mutation | `test_probe_sidecar_without_reinstate_is_decl_invalid` |
| L34 | Rename moves the sidecar (no orphaned declaration) | `test_rename_leaving_sidecar_at_old_stem_is_decl_missing` |
| L35 | Signal-handled restore — **sweep CLI** entry point, **SIGTERM leg** (the leg CPython does not unwind by default; SIGINT registration shapes the exit path only — residual nil, §1.4 prose; r4 CO-P2-3) | `test_sigterm_mid_sweep_restores_tree` (subprocess run of the **sweep CLI** on a slow fixture) |
| L36 | Signal-handled restore — **gate CLI** entry point, **SIGTERM leg** (§1.4 mechanism 2's second, independent handler; SIGINT residual stated as §1.4 prose; r4 CO-P2-3) | `test_gate_sigterm_restores_tree_and_exits_nonzero` (subprocess run of the **gate CLI** on a slow fixture pair; asserts nonzero exit AND clean tree) |
| L37 | Scope facts always stated (base sha, head sha, counts — in the `PASS[scope]` line and the JSON) | `test_scope_facts_present_in_pass_line_and_json` |
| L38 | Deleted-test-file exemption is real and visible | `test_deleted_test_file_is_exempt_and_visible` |
| L39 | `--repin` appends a well-formed `<timestamp> <digest>` record (parseable by the canonical block's awk extraction) | `test_repin_appends_record_in_pin_format` |
| L40 | No-legal-target exemption: owner-allowlist-sourced (base-ref-read, §1.2), verified against the recomputed closure both ways (+ probe exclusion + candidate-authored refusal) | `test_no_legal_target_allowlist_verified_against_recomputed_closure` (accept leg: allowlisted fixture pair with empty legal-candidate closure ⇒ loud `EXEMPT-NO-LEGAL-TARGET`; reject leg: allowlisted pair whose chain conftest imports a production module — the `tests/arb_memory` shape, §1.2 — ⇒ `DECL-INVALID` naming the legal candidates; probe leg: allowlisted `PROBE_PROVENANCE` pair ⇒ `DECL-INVALID`; candidate-authored leg: a sidecar `no_legal_target` key ⇒ `DECL-INVALID` naming the allowlist as the only exemption authority) |
| L41 | `EXEMPT-AST` emission is real and visible (r4 CO-P2-2) | `test_ast_exempt_pair_emits_visible_pass` (delete the emission ⇒ the exempt pair vanishes from the record while exit stays 0 ⇒ RED) |
| L42 | JSON summary carries per-pair computed refused surface, environment facts, and per-pair durations; pairs run and report in deterministic (sorted repo-relative path) order (r4 CO-P2-2) | `test_json_summary_fields_and_deterministic_pair_order` (delete any field or the ordering ⇒ RED) |

Mechanisms this plan relies on but does not own, cited not re-proved: the inherited sweep refusals
(dirty tree `mutation_sweep.py:91-101`; zero-collected `:136-142`; count equality `:151-156`;
anchor discipline `:104-124`) — already exercised by the sweep's history and re-exercised through
L15's propagation path.

## 5. File structure

- **Modify `scripts/mutation_sweep.py`** — `expect_failed` support, exit-code-checked
  baseline-green, per-id vitality + skip-vacuity refusals, import-resolution check, structured
  results, restore-by-hash, signal-handled restore boundary (§1.4, L35), `--json`. CLI and
  existing spec format stay backward compatible (`expect_failed` optional at the CLI layer; the
  *gate* requires it).
- **Create `tests/test_mutation_sweep_expect_failed.py`** — sweep-extension tests (L22–L26, L29,
  L35).
- **Create `scripts/changed_test_mutation_gate.py`** — the validator: repo/base resolution,
  scoping, closure and import-surface computation (§1.2), declaration loading/validation
  (including the allowlist-sourced no-legal-target verification, §1.2), per-pair sweep
  orchestration, residue tree check with tracked/untracked
  reasons (§1.4), SIGINT/SIGTERM handlers registered in `main()` (§1.4 mechanism 2, L36),
  protocol emission, JSON summary, sweep-pin verification, `--repin`. Written as importable
  functions + `main()` so every branch is unit-testable.
- **Create `scripts/changed_test_mutation_gate.py.sha256`** — the digest sidecar (gate `--repin`
  appends records in the `gate_runner.py:124-127` line format; format pinned by L39).
- **Create `scripts/mutation_gate_exemptions.json`** — the owner-reviewed no-legal-target
  allowlist (§1.2; lands **empty**; read from the base ref at gate time via `git show`; entries
  are owner-landed through review, never candidate-authored — r4 sol option 2).
- **Create `tests/test_changed_test_mutation_gate.py`** — the deny-proof suite over throwaway
  fixture git repos (L1–L21, L28, L30–L34, L36–L42). Fixture repos commit a `.gitignore` covering
  `__pycache__/` and `.pytest_cache/` so their sweeps see clean trees, as the real repo does
  (`.gitignore:2,6`).
- **Create the three sidecars for the pairs this slice's own diff puts in scope (Task 6 step 3;
  r3 CO-P2-5):** `tests/test_mutation_sweep_expect_failed.mutations.json`,
  `tests/test_changed_test_mutation_gate.mutations.json`, and
  `tests/defect_hunts/test_gate_assertions.mutations.json` — the third is the r3 CO-P1-1 worked
  example (its target is legal only under the import-surface rule).
- **Modify `tests/defect_hunts/test_gate_assertions.py`** — register the two new test modules in
  `GATE_TEST_FILES` (`:36-40`) **and** extend the guard for the gate-exit shape (Task 6): new
  bare-refusal markers (`returncode != 0`, `returncode == 1`, `exit_code == 1` …) and code-side
  markers (FAIL-line/reason-string asserts), plus the synthetic-offender extension of
  `test_the_guard_itself_is_not_vacuous` (`:102-135`). Without the extension, registration is
  vacuous for this gate's assertion shape (`_BARE_REFUSAL_MARKERS` `:43-50` is shaped for the
  claim-gate outcome object) — r1 CO-P1-5.
- **Create `docs/probe-relanding-convention.md`** — the re-landing convention (Task 5).
- **Create `docs/superpowers/specs/snippets/mutation-gate-ci-job.yml`** (name indicative) — the
  owner-gated CI job snippet; no live workflow file is touched.
- **Modify `docs/pipeline-operating-manual.md`** — the integration-step text, landed at PROPOSED
  strength pending owner co-sign (Task 8; doctrine-strength co-sign rail).

Not in this plan, by design: anything touching `claims`/`attestations`/`lease_lanes`, the bridge
(including `gate_runner.py`), any DSN, the close-reconcile, or the spot-check sampler (Slice 2);
any live workflow enable (owner); the node suite.

---

### Task 1: `expect_failed` binding, causality controls, structured results, restore boundary in `mutation_sweep.py`

**Files:**
- Modify: `scripts/mutation_sweep.py`
- Create: `tests/test_mutation_sweep_expect_failed.py`

**Interfaces:**
- Spec entries gain optional `"expect_failed": ["tests/foo/test_bar.py::test_baz", ...]`.
- `parse_pytest_output` additionally reports `passed` and `skipped` counts (it already parses the
  categories, `mutation_sweep.py:59,72-75`; they are currently summed away) **and harvests the
  `PASSED` and `SKIPPED` node ids from the `-rA` short summary** (the failed-id harvest at
  `:76-80` gains passed-id and skipped-id siblings; the skipped harvest is what lets L29 split
  the not-collected case from the skipped case, r3 CO-P2-3). `RunResult` additionally carries the
  **subprocess exit code** — today `run()` discards `returncode` and keeps only parsed text
  (`mutation_sweep.py:132-134`), which is exactly the hole r2 SOL-F2 executed: one passing test
  plus an unparsed internal-error line reads as green.
- **Baseline controls (new refusals, all `SweepRefused` with named reasons):**
  (a) **baseline green requires pytest exit code 0** — the explicitly justified singleton (exit
  1 = failures, 2 = interrupted, 3 = internal error, 4 = usage, 5 = no tests: none is the
  comparison the report claims) — *and* `baseline.failed` empty, the refusal naming the
  pre-existing failures — a gate whose job is proving fail-ability must not certify kills on a
  pre-red or internally-errored baseline (GROK-P1-2; r2 SOL-F2; port of the green-control at
  `coverage_mutation_sweep.py:166-179,508-518`);
  (b) **per-id vitality, reasons split (r2 CO-P1-2, L29; split r3 CO-P2-3):** every declared
  `expect_failed` id must appear in the baseline's PASSED ids — and the refusal names the actual
  defect, not a blanket one. An id absent from the baseline's *collected* ids (PASSED ∪ FAILED ∪
  SKIPPED ∪ ERROR, all parsed from `-rA`) refuses
  `NAMED-TEST-NOT-COLLECTED — <ids> match no collected node id; declare the exact collected form
  (parametrised: ::test_x[case]; unittest: ::Class::test_x); collected near-misses: <ids sharing
  the declared function name>` — the remedy is fixing the declaration. An id collected but
  skipped refuses `NAMED-TEST-SKIPPED — <ids> skipped at baseline; this environment cannot
  demonstrate fail-ability for this declaration` — the remedy is the environment (e.g. export
  `ARB_MEMORY_DSN`, §1.2). One string covering both remedies was the misattributed-reason class
  at message strength (r3 CO-P2-3). This is the load-bearing form of skip-vacuity: the DSN skips
  the gate must survive are **fixture-level**
  (`tests/arb_memory/conftest.py:83-87,109-113` — `pytest.skip` inside `scratch` /
  `empty_schema_conn`), so a partially-skipped pair passes every file-level control and, under
  v2's rules, produced a *false* `WRONG-MECHANISM` blaming the author's declaration when the
  truth was "your named test did not run here". The NOT-COLLECTED leg subsumes the typo'd or
  renamed-node-id case and makes Task 2's "resolves" unambiguous. The check applies exactly to
  entries carrying `expect_failed`; the gate's test command always includes `-rA` so per-id
  outcomes are parseable, and a declared pair run without parseable per-id outcomes refuses —
  fail-closed, not fail-open. `baseline.passed == 0` remains as the degenerate whole-file
  refusal ("baseline passed 0 tests — all skipped or empty; this environment cannot demonstrate
  fail-ability", L25) — subsumed for declared pairs, retained for CLI/`binding: none` runs;
  (c) **import-resolution check:** for each distinct mutation target under a `src/` tree, resolve
  its top-level package with the sweep's interpreter and assert the imported `__file__` lies under
  the sweep root; mismatch ⇒ refuse (port of `coverage_mutation_sweep.py:32-34,152-163`; the
  CoW-worktree masking case, §1.2). PYTHONPATH is forced to `<repo>/src` for the check and for
  test runs; the assert — not the forcing — is the control, since an editable-install meta-path
  finder can beat PYTHONPATH.
- **Kill rule:** killed ⇔ `expect_failed ⊆ (mutated.failed − baseline.failed)` and `expect_failed`
  non-empty. (With the baseline required green the set difference equals `mutated.failed`; the
  difference form is kept as defence in depth.) A red run missing a named id records
  `"wrong_mechanism": True` with `"missing_expected": [...]` (and is not a kill). Entries without
  `expect_failed` keep the old any-failure rule (CLI compatibility) but are flagged
  `"binding": "none"` in results so the gate can refuse them (L14).
- **Restore boundary (§1.4):** revert becomes hash-verified — capture `sha256` per mutated file
  before mutation, assert it after the `git checkout --` inside the per-mutation `finally`
  (pattern: `coverage_mutation_sweep.py:240-246`), refuse on mismatch. The CLI entry points
  (`main()` in both the sweep and the gate — the §1.4 exhaustive two-handler enumeration; this
  task implements the sweep's, deny-proved by L35; Task 4 implements the gate's, deny-proved by
  L36 — **not** module import, since tests and the gate import `run_sweep` as a library)
  register SIGINT/SIGTERM handlers that raise, so the `finally` restore runs before exit
  (CPython's default SIGTERM disposition terminates without unwinding; the SIGINT leg's residual
  is nil and the deny-proofs are TERM-only — §1.4, r4 CO-P2-3). What this cannot
  guarantee — SIGKILL/OOM/power loss — is stated in §1.4 and §10, not papered over.
- New `--json <path>` writes the results list; exit codes unchanged (`mutation_sweep.py:34`).

**Steps:**
- [ ] **Step 1: failing tests first.** In `tests/test_mutation_sweep_expect_failed.py`, against
  tmp git fixture repos (source file + test file + `.gitignore`, committed): (a) a mutation whose
  `expect_failed` names `test_a` but which in fact reds only `test_b` is reported
  `wrong_mechanism` with `missing_expected` — assert the specific result fields, never exit code
  alone (L22); (b) a correct binding is a kill; (c) restore-hash mismatch (simulated via a
  monkeypatched restore that leaves edited bytes) refuses naming the file (L23), and the fixture
  additionally asserts the reported final tree state — detection is not recovery; the recovery
  companion is the gate-level L28 (r2 SOL-F3); (d) a fixture whose named test already fails at
  baseline refuses naming the pre-existing failure (L24); (e) a fixture whose baseline reports at
  least one pass but exits nonzero **without** a parsed `FAILED`/`ERROR` line (monkeypatched
  runner returning a green parse with exit 3) refuses naming the exit code (L24's second test —
  the r2 SOL-F2 shape); (f) a fixture whose paired file is entirely `pytest.mark.skip`ped refuses
  with the file-level skip-vacuity reason (L25); (g) a fixture where the *declared* id depends on
  a skipping fixture while a sibling test passes refuses with `NAMED-TEST-SKIPPED` (L29's
  environment leg — the fixture-level-skip shape of `tests/arb_memory/conftest.py:83-87`);
  (g2) a fixture declaring a bare function name for a parametrised test refuses with
  `NAMED-TEST-NOT-COLLECTED` naming the collected near-misses (L29's id-form leg, r3 CO-P2-3);
  (h) an import-resolution mismatch (fixture: force PYTHONPATH at a decoy tree so the package
  resolves outside the sweep root) refuses (L26); (i) SIGTERM delivered mid-sweep to a subprocess
  **sweep-CLI** run on a slow fixture exits nonzero **and leaves the tree clean** (L35 — the
  sweep-CLI leg; the gate-CLI leg is L36, Task 4 step 1); (j) old-format specs still run, flagged
  `binding: none`.
- [ ] **Step 2: implement.** `parse_pytest_output` already collects failed node ids (`:76-80`);
  the superset check is set arithmetic against that, the passed-id and skipped-id harvests are
  its siblings, and the exit code is one field carried instead of dropped (`:132-134`).
- [ ] **Step 3: verification.** `pytest tests/test_mutation_sweep_expect_failed.py -q` green;
  inject-revert L22, L24 (both tests), and L29 (both legs) immediately (comment out the superset
  check / the exit-code + baseline-green checks / the per-id vitality check, observe the named
  test RED, restore). Record observed output, not predictions.

### Task 2: Declaration sidecar schema + loader

**Files:**
- Create: `scripts/changed_test_mutation_gate.py` (loader + validation; grows in Tasks 3–4)
- Create: `scripts/mutation_gate_exemptions.json` (the owner allowlist, §1.2 — lands empty)
- Test: `tests/test_changed_test_mutation_gate.py`

**Schema** (co-located sidecar, `tests/foo/test_bar.py` ↔ `tests/foo/test_bar.mutations.json`):

```json
{
  "schema": 1,
  "mutations": [
    {
      "id": "M1",
      "kind": "defect",
      "label": "delete the lane check",
      "file": "src/agent_redis_bridge/some_module.py",
      "find": "<exact anchor>",
      "replace": "<mutated bytes>",
      "expect_failed": ["tests/foo/test_bar.py::test_lane_check_refuses_by_name"]
    }
  ]
}
```

`kind` is `"defect"` (ordinary declared defect) or `"reinstate"` (a probe's remediated defect,
Task 5).

**Node-id form (r3 CO-P2-3):** every `expect_failed` entry is the **exact node id pytest
collects** — the repo-relative path, then `::`-separated components including the test-class name
for unittest-style tests (`tools/authorbench/tests/test_jail.py::<TestCase>::test_x` — the L30
worked example is itself unittest-based, `test_jail.py:4`) and the full parametrisation suffix
for parametrised tests (`tests/foo/test_bar.py::test_baz[case-a]`). There is **no prefix
expansion**: a bare `::test_baz` for a parametrised test matches no collected id and refuses
`NAMED-TEST-NOT-COLLECTED` naming the collected near-misses (Task 1(b), L29) — never the
environment.

**The no-legal-target exemption (§1.2; r3 CO-P1-1 → r4 CO-P2-1 + SOL-R4-F1, owner-selected sol
option 2):** a sidecar carries `mutations` **only** — a `no_legal_target` key in a sidecar is
`DECL-INVALID` outright (exemption authority is never candidate-authored; L40 candidate-authored
leg). The exemption lives in the owner-reviewed allowlist
`scripts/mutation_gate_exemptions.json`, which the gate reads from the **base ref**
(`git show <base>:scripts/mutation_gate_exemptions.json`; absent at the base ⇒ empty allowlist —
the first-landing exception is §1.1's):

```json
{
  "schema": 1,
  "exemptions": [
    {
      "path": "tests/e2e/test_spine.py",
      "reason": "harness-self-test",
      "subject": "E2EResult/E2EStatus semantics of tests/e2e/spine.py"
    }
  ]
}
```

`reason` is one of `"harness-self-test"`, `"environment-contract"`, or
`"deselected-placeholder"`; `subject` is a non-empty one-line statement of what the file tests.
Because this authority is deliberately read from the merge base, a revocation made only at the
candidate tip does not govern that same merge: the base entry remains effective until its removal
lands in a prior base. This revocation lag is an accepted, visible residual of base-ref authority;
it is not instant revocation.
The gate honours an entry **only after verifying, from its own recomputed closure (Task 3;
§1.2's precise definition with the enumerated resolution roots), that the pair has no legal
candidate**; if one exists, the exemption is `DECL-INVALID` and the refusal names the legal
candidates found — the reject-leg fixture is the real-repo `tests/arb_memory` shape (chain
conftest importing a production module, §1.2's worked example). A pair carrying
`PROBE_PROVENANCE` may never be exempted — probes reinstate production defects by construction
(`DECL-INVALID`; L40 probe leg). Accepted ⇒
`PASS[pair:<path>]: EXEMPT-NO-LEGAL-TARGET (<reason>) — <subject>`, recorded in the JSON summary.

**Validation rules (each a distinct `FAIL[pair:<path>]: DECL-INVALID — <reason>`):**
- `mutations` present and non-empty; a `no_legal_target` key present ⇒ `DECL-INVALID` naming the
  allowlist as the only exemption authority (L40 candidate-authored leg); every mutation entry
  has non-empty `expect_failed` (L12);
- every `expect_failed` id resolves to a node id **inside the paired test file** (L12) — a
  textual membership check at the loader (the path component must equal the paired file;
  exact-form rules above); execution vitality (collected and passed at baseline) is the sweep's
  per-id control (L29), which is what makes "resolves" unambiguous (r2 CO-P1-2);
- `file` does not resolve onto the pair's **refused import surface** (§1.2; L13 — the pair's own
  chain conftest is the canonical refused fixture; the legal/refused boundary is pinned by L30,
  the allowlist-sourced no-legal-target verification by L40);
- allowlist entries, when matched to an in-scope pair: `reason` in the enum, `subject` non-empty,
  recomputed-closure condition verified (no legal candidate), no `PROBE_PROVENANCE` in the
  paired file (L40);
- `kind` present and valid (L32);
- anchor/no-op discipline is *not* re-validated here — the sweep already refuses those
  (`mutation_sweep.py:104-124`); the loader validates only what the sweep cannot see.

**Steps:**
- [ ] **Step 1: failing tests** for each validation rule, asserting the specific reason string
  (never a bare "invalid") — L12/L13 (all parametrised surface legs), the `kind` rule (L32), the
  boundary fixtures (L30: a declaration for the authorbench `test_jail.py` pair targeting
  `tools/authorbench/authorbench/jail.py` is accepted while one targeting its own surface
  refuses; and the meta-test shape — a fixture pair mirroring
  `tests/defect_hunts/test_gate_assertions.py`, whose declaration targets an **unimported**
  sibling test file, is accepted and sweeps), and the no-legal-target legs (L40: allowlist
  accept / reject naming the legal candidates on the chain-conftest-imports-production shape /
  probe exclusion / candidate-authored `no_legal_target` refusal).
- [ ] **Step 2: implement** `load_declaration(test_file: Path, repo: Path, closure: PairClosure,
  exemptions: Allowlist) -> Declaration | Exemption | GateFail` — the closure carries both the
  refused surface and the legal-candidate set (Task 3); the allowlist is the base-ref-read
  object, never a worktree read.
- [ ] **Step 3: verification.** Unit tests green; inject-revert L12, L13 (chain-conftest and
  imported-module legs at minimum), L30 (both tests), L32, and L40 (all four legs).

### Task 3: Changed-pair scoping — repo/base resolution, patterns, closure computation, pair-aware AST exemption

**Files:**
- Modify: `scripts/changed_test_mutation_gate.py`
- Test: `tests/test_changed_test_mutation_gate.py`

**Behaviour:**
- Resolve repo: `--repo` (default cwd) must equal `git -C <repo> rev-parse --show-toplevel`
  (else exit 2, usage refusal — L31). Every git subprocess uses `git -C <repo>`; nothing depends
  on ambient cwd (AGY-01 discipline).
- Resolve base: `--base` mandatory (argparse) → `git -C <repo> merge-base HEAD <base>`.
  Unresolvable ⇒ `FAIL[scope]` (L1); merge-base == HEAD ⇒ `FAIL[scope]: base resolves to HEAD —
  wrong invocation or nothing to gate` (L2). On success emit
  `PASS[scope]: base=<sha> head=<sha> changed_files=<n> test_pairs=<m> exempt=<k>` — the scope
  facts are always in the record, and that property is deny-proved (L37, r3 CO-P2-2).
- Changed files: `git -C <repo> diff --name-status -M <merge-base>...HEAD` (rename-aware,
  commit-to-commit). Pair in scope when either member changed (L5, L6). Deleted test file ⇒
  exempt, emitted as `PASS[pair:<path>]: EXEMPT-DELETED` (L38); renamed ⇒ in scope at the new
  path, AST-comparable against the old path (L9) — **and the declaration must move with it**: an
  old-stem sidecar at merge-base with no new-stem sidecar at HEAD ⇒ `DECL-MISSING` (L34; r2
  cold-Opus P2-6).
- **Closure and surface computation (§1.2; r3 CO-P1-1 → r4 CO-P2-1):** `pair_closure(test_file,
  repo) -> PairClosure` — carrying **both** the **refused surface** (the paired file, every
  `conftest.py` on its conftest chain from repo root down to its directory, every package
  `__init__.py` on its package path inside a scoped tree, plus the transitive static import
  closure restricted to files resolving under `SCOPED_TEST_TREES` roots) **and** the
  **legal-candidate set** (closure members off the refused surface and not scoped-tree
  test/support, per §1.2's definitions — the set L40's reject leg names). Implementation is
  stdlib-only and never executes the modules: an `ast` walk collects `import`/`from … import`
  module names **and `pytest_plugins` assignments** (from the paired file and each chain
  conftest); dotted names are resolved to repo paths against the **enumerated resolution roots —
  repo root, `src/`, and the tool package dirs (`tools/*/` package roots)** (r4 CO-P2-1:
  `src/`-rooted names like `arb_memory.mcp.config` from `tests/arb_memory/conftest.py:15-16`
  resolve and are visible as legal candidates); recursion continues only into files that
  resolved under a scoped tree. Names resolving to no repo file (stdlib, site-packages) are
  ignored — they are not repo files and cannot be mutation targets. Each pair's computed surface
  is recorded in the JSON summary so a reviewer can see exactly what was refused (L21 covers
  check fidelity; the field's presence is deny-proved by L42, r4 CO-P2-2).
- Tree constant: `SCOPED_TEST_TREES` per §1.2, a single named constant.
  `test_pattern_set_matches_ci_suite_trees` enumerates it against
  `.github/workflows/ci.yml.disabled:95-104` + `tools/faba/tests/`, mapping CI's
  `tools/authorbench` rootdir argument to the `tools/authorbench/tests/` subtree (r2 CO-P1-3);
  removals red immediately; additions are caught by review of the protected CI file. (v3's
  `REFUSED_TARGET_TREES` constant is gone with the tree rule — §1.2, r3 CO-P1-1.)
- AST exemption, **pair-aware (r1 SOL-F1)**: applies only when the test file is the sole changed
  member; parse base bytes (`git show <merge-base>:<path>`) and HEAD bytes with `ast.parse`;
  exempt iff `ast.dump(...)` identical; any parse failure ⇒ not exempt (L8). Emit
  `PASS[pair:<path>]: EXEMPT-AST — comment/whitespace-only change` so exemptions are visible —
  the emission itself is deny-proved (L41, r4 CO-P2-2).
  L7 pins tightness (one-token behavioural edit is not exempt).

**Steps:**
- [ ] **Step 1: failing tests** in fixture git repos: L1, L2, L3 (control: a committed change to a
  fixture test file is reported in scope — the "prove the harness can see a failure" check, same
  reasoning as `coverage_mutation_sweep.py:508-518`'s control), L5, L6, L7, L8, L9, L31 (a
  `--repo` pointing at a subdirectory or a non-toplevel path ⇒ exit 2), L34 (rename leaving the
  sidecar at the old stem ⇒ `DECL-MISSING`), L37 (delete any scope fact from the line or the
  JSON ⇒ RED), L38 (deleted-file exemption emits its visible PASS), L41 (an AST-exempt fixture
  pair emits its visible `EXEMPT-AST` PASS; delete the emission ⇒ RED), the pattern enumeration,
  and the closure/surface-computation fixtures backing L13/L30/L40 (transitive import,
  `pytest_plugins`, package `__init__`, chain conftest — plus the resolution-root fixture: a
  chain conftest importing a `src/`-rooted production module yields a **legal candidate**, the
  L40 reject-leg shape, r4 CO-P2-1 alternative 3).
- [ ] **Step 2: implement.**
- [ ] **Step 3: verification.** Suite green; inject-revert L1, L2, L3, L5, L6, L7, L31, L34,
  L37, L38, L41.

### Task 4: Gate assembly — protocol emission, sweep orchestration, tree check, signal handlers, pin machinery

**Files:**
- Modify: `scripts/changed_test_mutation_gate.py`
- Create: `scripts/changed_test_mutation_gate.py.sha256` (via the gate's `--repin` at the end)
- Test: `tests/test_changed_test_mutation_gate.py`

**Behaviour:** startup order: repo/base resolution (Task 3) → **sweep-pin check**: sha256 of
`scripts/mutation_sweep.py` must equal the `MUTATION_SWEEP_SHA256` constant embedded in the gate's
own (invoker-pinned) bytes, else `FAIL[sweep-pin]` and exit 1 — the pin is transitive, so the kill
rule cannot be neutered without a visible gate repin (L19; folds grok P2-1) → **exemption-allowlist
read** from the base ref (`git show <base>:scripts/mutation_gate_exemptions.json`, §1.2 — the
candidate worktree's copy is never consulted; absent at the base ⇒ empty allowlist, §1.1's
first-landing exception) → per-pair scoping, closure computation, and declaration validation
(items 1–2) → budget (item 3 — the
declared-mutation count exists only once declarations are parsed; r2 cold-Opus P2-3) → per-pair
sweeps (item 4), in deterministic sorted repo-relative path order (deny-proved by L42, r4
CO-P2-2):
1. `FAIL[pair:<path>]: DECL-MISSING — …` if no sidecar (L4), including sidecar-deleted (L5), the
   rename-orphan case (L34), and the relanded-probe case (L18, wired in Task 5).
2. Declaration validation per Task 2 (`DECL-INVALID` reasons, including `binding: none` entries —
   the gate requires the binding even though the CLI tolerates its absence, L14 — and the
   allowlist-sourced no-legal-target verification against the recomputed closure, L40).
3. Budget: total declared mutations across in-scope pairs > `MAX_MUTATIONS_PER_RUN` (64) ⇒
   `FAIL[budget]: declared=<n> ceiling=64 — split the change` and **no sweep runs** (L16, L17).
4. Import `run_sweep` and run it per pair (direct call, pre-split `list[str]` test command —
   `mutation_sweep.py:128`; AGY-S3P-03) with
   `test_cmd = [sys.executable, "-m", "pytest", <paired_file>, "-q", "-rA", "--no-header"]` —
   `<paired_file>` **repo-relative** (§1.3, r2 cold-Opus P2-8); `sys.executable` is the project
   venv python by the canonical invocation, fail-closed otherwise (§1.3). Result mapping, one
   check per pair:
   - all mutations killed via their named, newly-failing tests ⇒
     `PASS[pair:<path>]: <k> mutations, all RED via declared tests`
   - any survivor ⇒ `FAIL[pair:<path>]: SURVIVOR <mut-id> "<label>" — paired suite green with the
     declared defect present` (L10)
   - red-but-wrong-mechanism ⇒ `FAIL[pair:<path>]: WRONG-MECHANISM <mut-id> — red, but not via
     <missing expected ids>` (L11)
   - `SweepRefused` ⇒ `FAIL[pair:<path>]: SWEEP-REFUSED — <the sweep's own reason, verbatim>`
     (L15; zero-collected, pre-red or nonzero-exit baseline, named-id-not-collected,
     named-id-skipped, all-skipped, restore-hash and import-resolution reasons all surface here)

   **After every pair — whatever its outcome, including an exception escaping `run_sweep` — the
   gate runs the §1.4 tree check** (`git -C <repo> status --porcelain`, status codes parsed):
   tracked residue ⇒ `FAIL[tree]: DIRTY-AFTER-SWEEP — <paths> — run: git -C <repo> checkout --
   <paths>; confirm clean with git -C <repo> status --porcelain; then re-run the gate`;
   untracked residue ⇒ `FAIL[tree]: UNTRACKED-RESIDUE — <paths> — likely left by the gated test
   run; preview with: git -C <repo> clean -n -- <paths>; remove with: git -C <repo> clean -f --
   <paths>` (r3 CO-P2-4 — `git checkout --` does not work on untracked paths). Either reason
   **aborts the remaining pairs**, never cascading into false dirty-tree refusals (L28, both
   tests).
5. Exit per the §3 contract (L20); `--json` summary carries every check verbatim plus scope facts
   (L37), environment facts (interpreter path, `ARB_MEMORY_DSN` present yes/no), per-pair sweep
   results and computed surfaces, durations, and the tree-check outcome (L21 pins
   checks-vs-emitted fidelity; the presence of the surface/environment/duration fields and the
   deterministic pair order are deny-proved by L42, r4 CO-P2-2).
6. **`main()` registers SIGINT/SIGTERM handlers that raise, before any pair runs (§1.4 mechanism
   2; L36).** The gate imports `run_sweep` as a library, so the sweep CLI's handlers (L35) are
   **not in this process**: without the gate's own registration, `timeout`'s default TERM would
   terminate without unwinding the per-mutation `finally` and strand mutated bytes in the real
   integration checkout — the r2 recovery class on the production entry point (r3 SOL-R3-F1 /
   CO-P2-1). The deny-proof is TERM-only; the SIGINT residual is nil (§1.4 prose, r4 CO-P2-3).

**Canonical invocation** (this exact block also goes into the Task 8 manual text; `BASE=dev` at
integration, `main` for release merges):

```bash
BASE=dev
# 0. Summary path — explicit and self-contained; no ambient variable required
#    (r2 SOL-F1 / AGY-R2-02: v2's bare $SCRATCH expanded to /mutation-gate-summary.json).
SUMMARY_DIR="${SCRATCH:-/tmp/mutation-gate}"
mkdir -p "$SUMMARY_DIR"

# 1. Digest check — the pin is read from the INTEGRATION TARGET, not the candidate.
expected="$(git show "${BASE}:scripts/changed_test_mutation_gate.py.sha256" \
  | awk 'NF {pin=$NF} END {print pin}')"
actual="$(shasum -a 256 scripts/changed_test_mutation_gate.py | awk '{print $1}')"
[ -n "$expected" ] && [ -n "$actual" ] || { echo "GATE PIN UNREADABLE" >&2; exit 3; }
[ "$expected" = "$actual" ] || { echo "GATE DIGEST MISMATCH" >&2; exit 3; }

# 2. The gate (clean tree required; gates HEAD against --base).
.venv/bin/python scripts/changed_test_mutation_gate.py \
  --base "$BASE" --json "$SUMMARY_DIR/mutation-gate-summary.json"
```

The `awk 'NF {pin=$NF} END {print pin}'` extraction takes the digest field of the last non-empty
record, immune to the `<timestamp> <digest>` line shape and trailing newlines that broke v1's
`tail -1` → `--pin` (r1 SOL-F2/CO-P1-1). The non-empty guard closes the empty-equals-empty pass
(a missing sidecar *and* a missing gate file both yield `""`, and `[ "" = "" ]` is true — r2
cold-Opus P2-4); `${SCRATCH:-/tmp/mutation-gate}` + `mkdir -p` makes the block runnable with
`SCRATCH` absent (r2 SOL-F1). For the merge that introduces or changes the gate machinery itself,
`$BASE` has no (current) sidecar: for that landing only, the digest step is against the
candidate's own reviewed diff, and the manual text names this exception explicitly (§1.1).

**Steps:**
- [ ] **Step 1: failing tests** for L4, L10, L11, L14, L15, L16, L17, L19, L20, L21, L28 (both
  tests), L36, L39, and L42 against fixture repos (a hollow test + a declaration that should kill
  it ⇒ survivor path; a mutation redding an unrelated test ⇒ wrong-mechanism; a pair collecting
  nothing ⇒ refusal path; an edited fixture copy of `mutation_sweep.py` ⇒ sweep-pin path; a
  monkeypatched per-pair restore that leaves edited bytes ⇒ the distinct `FAIL[tree]` **and** no
  further pairs run — L28's simulated-failed-restore fixture; a fixture pair whose test run
  plants an untracked file ⇒ `UNTRACKED-RESIDUE` with the `git clean -n` guidance and the abort
  — L28's second test; SIGTERM delivered mid-sweep to a subprocess **gate-CLI** run on a slow
  fixture pair ⇒ nonzero exit AND clean tree — L36, the gate-process leg of §1.4 mechanism 2;
  `--repin` on a fixture sidecar ⇒ a `<timestamp> <digest>` record the canonical block's awk
  extraction parses back to the digest — L39; a multi-pair fixture run's JSON carries per-pair
  computed surfaces, environment facts, and durations, and lists pairs in sorted repo-relative
  path order — delete any field or the ordering ⇒ RED — L42). Assert check ids, reason
  substrings, JSON contents, and exit codes — never bare non-zero.
- [ ] **Step 2: implement.**
- [ ] **Step 3: end-to-end** on green and red fixture repos via the canonical block executed with
  `SCRATCH` **unset** in the environment (r2 SOL-F1), confirming both the gate result and the
  JSON summary at the defaulted path; then `--repin` and commit the sidecar.
- [ ] **Step 4: verification.** Suite green; inject-revert L4, L10, L11, L15, L16, L17, L19, L28
  (both), L36, L39, L42; record the observed JSON for one green and one red fixture run, plus
  per-pair durations (first budget measurement, Open question 3).

### Task 5: Probe re-landing convention (before the ledger pass — AGY-02)

**Files:**
- Create: `docs/probe-relanding-convention.md`
- Modify: `scripts/changed_test_mutation_gate.py` (PROBE_PROVENANCE detection, L18)
- Test: `tests/test_changed_test_mutation_gate.py`

**The convention** (mechanizes spec §6, lines 453–457: probe code dies with the exempt worktree,
is rehydrated from the package artefact inside the remediation diff, and becomes a permanent
regression test the mutation gate protects):

1. **Placement:** the relanded probe is an ordinary pytest module in the suite tree that owns the
   remediated code, following that tree's naming conventions. No probes-only quarantine folder.
2. **Provenance marker:** the module carries a module-level constant, AST-detectable without
   import:
   ```python
   PROBE_PROVENANCE = {
       "claim_id": "<claim id>",
       "probe_artefact_id": "<store artefact id>",
       "probe_artefact_version": 1,
   }
   ```
   Shape is validated by the gate (keys present, non-empty strings / positive int). **Store
   resolution is deliberately not performed** — the gate runs without any DSN of its own, and
   store-side fidelity checking belongs to Slice 2 (spec §10 row 2, line 681). The marker is a
   pointer at enforced-shape strength, not a verified store reference.
3. **The reinstatement declaration — stated at enforced strength (grok P2-2):** a changed test
   file containing `PROBE_PROVENANCE` **must** carry a sidecar with at least one mutation of
   `"kind": "reinstate"` whose `expect_failed` names the probe's tests (and may never be
   exempted via the no-legal-target allowlist — Task 2/§1.2, L40 probe leg). What the gate
   *enforces* is: marker ⇒ sidecar present, shape-valid, at least one `reinstate` mutation
   (L33), and every declared mutation kills via its named tests. Whether the `reinstate`
   mutation's find/replace genuinely reinstates *the remediated defect* — as opposed to some
   other killing edit — is **panel-read in the diff and Slice-2-verified, not gate-verified**;
   the convention doc states the intent ("`find` = fixed production bytes, `replace` = the
   defective bytes the probe demonstrated") as a review obligation, at exactly that strength.
4. **Honest limit in the doc:** byte-fidelity between the relanded module and the stored package
   is *not* claimed — rehydration legitimately adapts imports/fixtures to suite form. The
   behavioural link is the reinstatement mutation; the provenance pointer is for audit and for
   Slice 2's close/spot-check to resolve.

**Steps:**
- [ ] **Step 1: failing tests** — L18 both parts (marker without sidecar ⇒ `DECL-MISSING` reason
  naming the probe convention and linking the doc; marker shape invalid ⇒ its own reason), plus
  a probe sidecar with no `reinstate`-kind mutation ⇒ `DECL-INVALID` (L33).
- [ ] **Step 2: implement detection** (AST walk for the assignment; no import of the test module).
- [ ] **Step 3: write `docs/probe-relanding-convention.md`** — the four points, the sidecar
  example, the spec citations. Prose claims exactly what L18 + L33 + the sweep enforce, nothing
  more.
- [ ] **Step 4: verification.** Suite green; inject-revert L18 and L33.

### Task 6: Deny-proof the gate; extend and register the bare-refusal guard; the slice gates itself

All gate mechanisms now exist (Tasks 1–5), so the full ledger pass is well-ordered (AGY-02).

**Files:**
- Modify: `tests/defect_hunts/test_gate_assertions.py`
- Modify: `tests/test_changed_test_mutation_gate.py`, `tests/test_mutation_sweep_expect_failed.py`
  (any fixes the guard demands)
- Create: `tests/test_mutation_sweep_expect_failed.mutations.json`,
  `tests/test_changed_test_mutation_gate.mutations.json`,
  `tests/defect_hunts/test_gate_assertions.mutations.json`

**Steps:**
- [ ] **Step 1: extend the guard so registration is meaningful (r1 CO-P1-5).** Add
  gate-exit-shaped bare-refusal markers (`returncode != 0`, `returncode == 1`, `exit_code == 1`,
  `.returncode` compared without an accompanying reason assert) and code-markers for this gate's
  reason shape (`FAIL[` substring asserts, `DECL-`/`SURVIVOR`/`WRONG-MECHANISM`/`SWEEP-REFUSED`/
  `DIRTY-AFTER-SWEEP`/`UNTRACKED-RESIDUE` string asserts); extend
  `test_the_guard_itself_is_not_vacuous` (`:102-135`) with a synthetic offender in the new shape
  and a compliant counterpart (L27). Then add both new test modules to `GATE_TEST_FILES`
  (`:36-40`) and run `pytest tests/defect_hunts/test_gate_assertions.py -q`; fix any offender it
  names.
- [ ] **Step 2: the ledger pass.** For every row L1–L42: delete or neuter the mechanism, run the
  named test, observe RED **with the row's mechanism named in the failure**, restore, observe
  green. **The two signal-handler rows are inject-reverted independently (r3 SOL-R3-F1):**
  delete only the gate `main()` handler — L36 REDs while L35 stays green; restore; delete only
  the sweep `main()` handler — L35 REDs while L36 stays green; restore. The cross-observation is
  the point: it demonstrates neither test rides the other entry point's mechanism. Per
  `docs/defect-classes/deny-proofs-need-adversarial-verification.md`, inject by editing
  conditions, never by `git checkout` over uncommitted work.
- [ ] **Step 3: the slice's own diff passes its own gate (r3 CO-P2-5; the r3 CO-P1-1 worked
  example, executed).** Author the three sidecars §5 names: for
  `tests/test_mutation_sweep_expect_failed.py`, a mutation in `scripts/mutation_sweep.py` (e.g.
  neuter the `expect_failed` superset check) with `expect_failed` naming L22's test; for
  `tests/test_changed_test_mutation_gate.py`, a mutation in
  `scripts/changed_test_mutation_gate.py` (e.g. invert the survivor→FAIL mapping) with
  `expect_failed` naming L10's test; for `tests/defect_hunts/test_gate_assertions.py`, the §1.2
  worked example — inject a bare-refusal offender function into `tests/test_claim_gate.py`, with
  `expect_failed` =
  `tests/defect_hunts/test_gate_assertions.py::test_gate_tests_pin_the_mechanism_not_merely_the_refusal`
  (a target that is legal **only** under the import-surface rule: the guard reads it as data and
  never imports it). Run the gate on the slice branch with `--base dev` via the canonical block;
  the run gates every pair the slice's diff put in scope — these three plus Task 7's
  `tests/test_seat_preflight.py` pair, four in total; observe the four `PASS[pair:…]` lines and
  record the JSON summary. The slice's own landing is the gate's second live-run subject (the
  first is Task 7).
- [ ] **Step 4: verification artefact.** The observed ledger table (mechanism, injection, named
  RED output line, restore confirmation — including the two-leg handler cross-observation) and
  the step 3 JSON go into the implementation-round evidence — same-turn tool output only, per
  the prediction-written-as-result rule.

### Task 7: First live run + honest discharge of the 1d-vi residual (r1 CO-P1-3)

**Files:**
- Create: `tests/test_seat_preflight.mutations.json` (sidecar; exact name per Task 2 mapping)
- Modify: `tests/test_seat_preflight.py` (two prose corrections — see below)

**The residual.** A prior closure-residuals review recorded:
*"Two test-prose claims describe a different RED mechanism than the one that fires… Trigger:
Slice 3 mutation gate or next edit of those tests."* Owning record: the r4 cold-Opus report's
P2-3 finding: the docstring at `tests/test_seat_preflight.py:2321-2323` names the name-assertion
as the RED mechanism while the traced mechanism is the `assert not ok` at `:2330` (via the
short-circuit at `scripts/seat-preflight:347-349`, per that report's trace — cited, not
re-derived); and `:2333-2334` credits a logically redundant assertion with discrimination it does
not add. The r4 trace is source-level only ("*Not executed (no shell)*").

**What the gate can and cannot demonstrate here — stated before the steps, because v1 overclaimed
it.** The sweep records the summary line and failed **node ids** only
(`mutation_sweep.py:76-80,157-158`) and discards pytest bodies after parsing (`:132-134`);
`expect_failed` binds at node-id granularity. The residual is an **intra-test** claim — *which
assertion inside one test* fires. So: (a) the gate run demonstrates node-level
red-for-the-stated-reason (the swapped-key defect kills via the named test — real, and the first
execution-grade evidence at that granularity); (b) the intra-test attribution is settled by a
**direct inject-revert pytest run outside the sweep**, whose traceback is captured and cited
verbatim. Correspondingly, the plan's class-discharge claim is scoped: the gate makes
prose-names-the-wrong-**test** mechanically FAILable; prose-names-the-wrong-**assertion** remains
reading/trace territory (also §10 Honest limits).

**Steps:**
- [ ] **Step 1: author the sidecar** for `tests/test_seat_preflight.py` with (at minimum) the
  swapped-key-argument mutation in `scripts/seat-preflight` (production surface — and a **legal
  target under §1.2's import-surface rule**: `tests/test_seat_preflight.py` drives
  `scripts/seat-preflight` by subprocess, never imports it, and `scripts/` resolves under no
  scoped tree), `expect_failed` pinned to
  `tests/test_seat_preflight.py::test_attestation_call_site_binds_worker_vantage`. Run the gate on
  a branch where the pair is in scope; observe
  `PASS[pair:tests/test_seat_preflight.py]` with the sweep report showing the kill via the named
  node id.
- [ ] **Step 2: settle the intra-test claim by execution.** Apply the same mutation manually, run
  `.venv/bin/python -m pytest "tests/test_seat_preflight.py::test_attestation_call_site_binds_worker_vantage" --tb=long -q`,
  capture the traceback showing **which assert line** fires, revert (hash-verified). This — not
  the sweep — is the execution-grade check on the r4 trace.
- [ ] **Step 3: correct the two prose claims** to match the observed mechanism (the report's
  suggested corrections at lines 118–120 are the starting point; the observed traceback is
  authoritative). **Path: standard, inside this slice's implementation review — the light path is
  not taken** (r1 cold-Opus P2-6: the sidecar is gate machinery, and
  `docs/pipeline-operating-manual.md:148-150` puts tier machinery in the never-light set; the
  whole Task lands under this slice's panel anyway). If the observed mechanism *disagrees* with
  the r4 trace, that is a new finding for the round, not a P2 fix.
- [ ] **Step 4: verification.** Gate run over the branch green for the pair; full
  `pytest tests/test_seat_preflight.py -q` green after the prose edits; the captured traceback in
  the evidence. This run doubles as the gate's first non-fixture demonstration and the plan's
  rival-instrument evidence for the review round
  (`docs/pipeline-operating-manual.md:163-183`), and its wall-clock is the second budget
  measurement (Open question 3).

### Task 8: Process wiring — manual text (co-sign flagged), CI-ready snippet, brief line

**Files:**
- Modify: `docs/pipeline-operating-manual.md`
- Create: `docs/superpowers/specs/snippets/mutation-gate-ci-job.yml`

**Steps:**
- [ ] **Step 1: manual text.** Add an integration-step subsection: before merging any candidate
  branch that changes test files, the orchestrator runs the canonical block (Task 4 — summary
  path, digest check from `$BASE`, then the gate) and the round close cites the JSON summary path
  + the verified digest. Include: the gate-machinery-change exception to the `$BASE` pin read
  (§1.1); the exemption-allowlist governance line (§1.2 — entries in
  `scripts/mutation_gate_exemptions.json` are owner-landed through review and read from the base
  ref, never taken from a candidate branch); the DSN posture line (§1.2 — export
  `ARB_MEMORY_DSN` when gating `tests/arb_memory/` pairs); and the §1.4 recovery instructions
  verbatim (a `FAIL[tree]` or a hard-killed run leaves
  mutated bytes: `git -C <repo> status --porcelain` to enumerate; `git -C <repo> checkout --
  <paths>` for tracked paths; `git -C <repo> clean -n -- <paths>` then `clean -f` for untracked
  residue; a final `status --porcelain` that MUST print nothing; then re-run the gate; never
  commit, stash, or merge from a checkout in that state).
  **Constitution-layer flag, per the doctrine-strength co-sign rail (CLAUDE.md):** drafted at
  REQUIRED strength but landed marked *proposed — pending owner co-sign*; it binds only on Mark's
  co-sign. The plan does not self-promote doctrine.
- [ ] **Step 2: CI snippet.** One job: checkout with `fetch-depth: 0` (merge-base needs history —
  `.github/workflows/ci.yml.disabled:48-53`), `uv sync --all-extras` (which provides the venv the
  gate runs under — in CI the environment question is solved at the job level, ci recipe
  `:64-65`), then the canonical block with `BASE` set from the PR base ref **materialised as a
  resolvable ref first** — a fresh `actions/checkout` has no local `dev`/`main`; the repo already
  learned this for merge-base (`git branch main origin/main`,
  `.github/workflows/ci.yml.disabled:54-59`), and the `git show "$BASE:…"` pin read **and the
  gate's base-ref allowlist read (§1.2)** need the same treatment (`git branch "$BASE"
  "origin/$BASE"` or `origin/$BASE` throughout — r2 cold-Opus
  P2-5) — and the DSN exports mirrored from the existing suite job (`:83-89`) so
  `tests/arb_memory/` pairs are gateable. Stored as a snippet, not a workflow: enabling CI is
  owner-executed and CI config is protected (CLAUDE.md overwrite gate). The snippet header states
  this.
- [ ] **Step 3: worker-brief line** (added to the manual subsection): dispatched workers changing
  test files run the gate locally with `--base <branch point>` before reporting done — noting
  that in a CoW-venv worktree the import-resolution refusal (L26) may fire, in which case the
  local run is advisory-refused and the merge-time run decides; the merge-time run is
  authoritative in all cases.
- [ ] **Step 4: verification.** `git diff` on the manual reviewed with the protected-file
  three-facts statement (deleted/moved/added) before commit; snippet lints as YAML.

---

## 6. What a green-on-mutation survivor gets logged as (Slice 2 is the first adversarial consumer)

When Slice 2's implementation lands its test files under this gate and a declared mutation
survives:

- **At the gate:** `FAIL[pair:<path>]: SURVIVOR <mut-id> "<label>" — paired suite green with the
  declared defect present`; gate exit 1; the JSON summary carries the check and the sweep report
  verbatim. Never a warning, never an exemption field. (And never a *false* SURVIVOR from an
  all-skipped pair — that is a `SWEEP-REFUSED` with the skip-vacuity reason, L25 — nor a false
  `WRONG-MECHANISM` from a fixture-skipped named id — that is `NAMED-TEST-SKIPPED`, L29; a
  mis-declared id is `NAMED-TEST-NOT-COLLECTED`, never blamed on the environment.)
- **In the round:** the FAIL is carried into the review round's decision record as a finding —
  presumptive P1 ("a landed test cannot see the defect it names"; final severity is the round's
  adjudication, but the *presumption* is P1 because this is exactly the
  four-suites-green-with-behaviour-deleted class the spec cites as Slice 3's justifying evidence,
  spec §10 lines 684–686). The merge is blocked until the pair goes RED-for-the-stated-reason or
  the declaration is corrected — and a corrected declaration must itself pass the gate, so the
  fix cannot be "declare something weaker" without that weakening being visible in the sidecar
  diff (which, per §1.2, is itself an in-scope change).
- **As evidence:** the sweep report for the surviving mutation *is* the reproduction — a
  demonstrated finding about the suite in exactly the red-before-remediate sense (spec §2), with
  no extra probe-construction round needed.

## 7. Review-loop exit conditions (Captured Loop rule 1 — defined at creation)

Findings against this plan's implementation are classed at adjudication; each class carries its
exit condition, fixed now:

- **Class V — gate vacuity** (a plan-mandated mechanism whose deletion leaves the gate green, or
  a deny-proof that passes against the wrong implementation): closes only by inject-revert
  demonstration of the affected ledger row (Task 6 form). **Loop exit for the class:** every
  ledger row L1–L42 demonstrated, and the latest round raises zero new Class-V findings. A
  Class-V finding *recurring* after its demonstrated closure escalates to the owner rather than
  entering another remediation round.
- **Class S — scoping misses** (base-ref, pattern-set, pair-mapping, closure/surface-computation,
  or exemption gaps): closes by a constructed miss case added to the fixture suite and observed
  RED-then-green. **Loop exit:** constructed cases for all raised misses green, the pattern
  enumeration and closure/surface fixtures green.
- **Class P — prose/precision** (doc or message wording, including gate FAIL-string precision):
  P2/P3; eligible for the light path once record-adjudicated — except where the change touches
  gate machinery, which is never-light (manual `:148-150`); **never blocks loop exit.**
- **Overall exit:** one round with zero new P0/P1 across Classes V and S, with the Task 6 ledger
  evidence and the Task 7 live-run evidence (gate run + captured traceback) both present in the
  record. No round-count-based close; no close while any ledger row is undemonstrated.

## 8. Out of scope

- **Slice 2 entirely:** close-reconcile re-resolution, the randomised spot-check sampler,
  `falsifier_kind` weighting (spec §10 row 2, line 681). This gate never reads the store.
- **Slices 1e–1h and all bridge/envelope surfaces:** no `handle_raw`, no refusal-code, no lease
  or claims interaction — and, per the §1.1 decision, **no change to
  `src/agent_redis_bridge/gate_runner.py`**. The gate is repo tooling.
- **Enabling any CI workflow** — owner-executed; this plan ships a snippet only.
- **Backfilling declarations for existing test files.** The gate is delta-scoped by design;
  coverage ratchets in as files change. A bulk backfill is a separate, owner-priced effort.
- **Gating edits to conftest/fixture/helper modules under the suite trees** (grok P2-3,
  residual-with-trigger): such edits can hollow a suite's fail-ability without touching any
  `test_*.py`, and this gate does not scope them — spec §10 row 3 says "changed test files", and
  widening scope to support modules is a ratchet decision for the owner. They are, however,
  **refused as mutation targets for any pair whose run loads them** (§1.2, L13), so they cannot
  be used to fake kills *by mutation* on the pairs they can ambiently break (the harness-side
  variant is a §10 limit). Trigger for the ratchet: the first incident where a support-module
  edit is found to have hollowed a gated test, or an owner scope decision.
- **Non-Python suites** (`tools/pi-sdk-host` node tests) and non-pytest harnesses.
- **`scripts/coverage_mutation_sweep.py`** and its standing hook — untouched (§2).
- **Judging test deletions or test quality beyond fail-ability** — panel work (spec §12 spirit:
  reading judgment stays where it is priced correctly).

## 9. Open questions

1. **Why CI was disabled is not recorded in any input this plan received.** The execution-surface
   choice (§1.1) does not depend on the answer, but the owner's CI re-enable decision does; the
   reason should be recorded when that decision is made.
2. **Exact manual wording strength** (Task 8) is drafted-not-bound until owner co-sign, per the
   doctrine-strength rail. If the owner prefers the gate as a close-side requirement (the audited
   close refusing without a gate summary artefact) rather than a manual step, that is a Slice 2
   integration and should be named there — this plan deliberately does not reach into the close.
3. **`MAX_MUTATIONS_PER_RUN = 64` is an engineering estimate**, and with run-gate gone there is
   no hosting timeout to tune — the budget question becomes "what does a real pair cost?".
   Task 4's fixture runs and Task 7's live run produce the first per-pair duration measurements
   (recorded in the JSON summaries); the ceiling is a single named constant so retuning is a
   one-line, reviewable change.
4. **Whether the import-resolution refusal (L26) fires on this fleet's CoW-mirror worktrees** is
   an empirical question Task 1's fixture can only simulate. The first real worktree-local gate
   run answers it; either answer is safe (fire ⇒ advisory refusal with the merge-time run
   deciding; no-fire ⇒ worktree-local runs are trustworthy).

## 10. Honest limits

- **Base-ref authority revocation lag.** The no-legal-target allowlist is intentionally read from
  the merge base. Therefore an entry revoked only at the candidate tip still governs that merge;
  revocation becomes effective only after the removal is part of a prior base. This is visible
  and intentional, not instant revocation.

- **Declared mutations test what authors declare.** A trivial-but-killing declaration passes the
  gate. Mitigations: the sidecar is in the reviewed diff; the `expect_failed` binding plus the
  baseline-green causality control force the declaration to engage the named tests with *new*
  failures; the import-surface refusal means the paired run's own harness cannot be the declared
  kill vector (L13); relanded probes must declare a `reinstate`-kind mutation (Task 5, L33). The
  residual — declarations that are honest, killing, and still weak — is precisely what Slice 2's
  spot-check and panel reading exist for (spec §9.5: green is necessary, never sufficient). One
  named sub-case (grok P1-2's second sketch): a mutation that reds the named tests via a
  *module-level ambient failure* (import bomb in a shared production import) satisfies the
  binding without assertion-level engagement; assertion-message binding is out of scope,
  panel-read in the sidecar diff.
- **The refused surface and the closure are computed statically; dynamic loads are invisible to
  both.** A scoped-tree module loaded only via `importlib` (the e2e harness's dynamic hunt
  loading, `tests/e2e/h2_harness.py:5-6`) is not on the computed surface, so a declaration could
  target it; the count-equality and baseline-green controls still bound what such a mutation can
  fake, and the sidecar is in the reviewed diff. `pytest_plugins` **is** parsed (§1.2), so the
  known static loading channels are covered; `importlib` strings are not resolvable without
  execution, and this plan does not pretend they are. Correspondingly, data/path/dynamic targets
  are never inferred absent from an empty static import closure (SOL-R4-F1's option-2 rider):
  an empty closure licenses nothing by itself — it is one *necessary* condition on an
  owner-reviewed exemption, below.
- **The no-legal-target exemption is owner authority, verified only at static-consistency
  strength (r4 SOL-R4-F1 + CO-P2-1; sol option 2).** The recomputed-closure check (L40) proves
  an exemption's *consistency with the static closure*, not its truth: an empty closure is
  vacuously consistent for any stdlib-only pair — including data-read meta-tests that *do* have
  legal targets (the `tests/defect_hunts/test_gate_assertions.py` shape, which Task 6 declares
  properly instead). What changed at r4: the claim is no longer candidate-authored — it enters
  only through the owner-reviewed allowlist read from the base ref, so a candidate branch cannot
  self-exempt, and the residual is owner misjudgment surfaced by the loud
  `EXEMPT-NO-LEGAL-TARGET` line and the allowlist diff at the entry's own landing — not
  candidate abuse. The expected population at landing is an **author-run estimate, recomputed by
  the implementor with the corrected classifier at build time (SOL-R4-F1)**: currently
  `tests/e2e/test_spine.py` and `tests/e2e/test_hermetic.py` (correct under the precise closure
  — their chain conftests carry no static repo imports), plus the four deselected implbench
  placeholders if ever edited (collection vitality keeps them out of legal-target; allowlist
  entry vs. the `SCOPED_TEST_TREES` ratchet is the owner's call at that first edit).
  `tests/arb_memory/test_mcp_sdk_contract.py` is **not** in the population (§1.2's worked
  example): its conftest-contributed `src/` candidates make any allowlist entry refuse — and,
  the accepted fail-closed residual (r4 CO-P2-1's reading-A consequence, taken knowingly): no
  mutation of those candidates can kill via its named ids (conftest import breakage is a
  collection error the sweep refuses, `mutation_sweep.py:151-156`), so a future behavioural edit
  to that file FAILs at the gate and forces an owner decision through review — loud and blocked,
  never silently exempt. None of these files is touched by this slice; the first behavioural
  edit exercises the branch for real.
- **The gate cannot distinguish a genuine kill from a harness engineered to red (r2 cold-Opus
  P2-7).** A candidate-supplied conftest (or test body) that manufactures RED for the named ids
  whenever the source differs from HEAD fakes kills without any refused *target* — L13 refuses
  conftest as a mutation target, not as a collaborator. Both the sidecar and the conftest are in
  the reviewed diff; that review is the control, stated at exactly that strength.
- **Until CI hosts it, invocation is procedural** (§1.1) — both the digest check and the gate
  run. The `$BASE`-sourced pin makes gate tampering *visible in review*, not invocation
  skippable. The manual step + close-citation make skipping visible in the record; they do not
  make it impossible.
- **The digest check and the execution are separate opens (TOCTOU, r2 sol F4).** Nothing binds
  the bytes hashed in canonical step 1 to the bytes Python loads in step 2; a same-host writer
  winning that race defeats the pin. Under the stated threat model such an actor can equally skip
  the manual command outright, so this stays a named limit rather than a mechanism; an
  invoker-created, hash-verified immutable copy retires it if it ever matters.
- **No sandbox on the chosen surface.** The gate runs with the invoker's environment and
  privileges; its containment is the sweep layer's refusals (dirty-tree, restore-by-hash,
  post-sweep clean assert), the §1.4 recovery boundary, plus code review — not an external
  fingerprint. This is the price of the surface change, paid consciously (§1.1 table).
- **Recovery is best-effort below SIGKILL** (§1.4). The finally + the two entry-point handlers +
  gate-level tree check restore or loudly report on every path they cover; SIGKILL, OOM-kill,
  and power loss cannot be caught and can leave mutated tracked bytes on disk with no FAIL
  emitted. The failure is loud on the next touch (the sweep's dirty-tree refusal,
  `mutation_sweep.py:91-101`) and the recovery commands are in the manual (Task 8); the residual
  — merging from a hard-killed checkout without re-running the gate — is procedural, same class
  as the invocation limit above.
- **The import-resolution check (L26) covers imported `src/` packages only** (Task 1(c); r2
  cold-Opus P2-2 note). A target invoked as a script by path — Task 7's own
  `scripts/seat-preflight` — is outside it; the expectation that path-invoked targets resolve
  inside the checkout under sweep is an argument from how the tests locate them, not a mechanical
  check. The merge-time run on the primary checkout is authoritative either way.
- **Wrong-but-resolvable bases beyond the HEAD case remain green-able** (grok P2-4 residual): L2
  refuses base ≡ HEAD, and the scope facts (base sha, counts) are always in the summary (L37),
  but a base that is merely *wrong* (e.g. an ancestor of the intended target) produces an
  honest-looking diff. The reader of the close evidence must check the stated base sha; CI
  hosting (where the base is the PR's base ref, mechanically) retires most of this residual.
- **The gate discharges the wrong-test class, not the wrong-assertion class** (r1 CO-P1-3):
  `expect_failed` binds at node-id granularity; prose misattributing which *assertion* fires
  inside a test remains catchable only by trace/traceback reading (Task 7 step 2's form).
- **The AST exemption trusts `ast.parse` equivalence.** Semantics-affecting changes invisible to
  `ast.dump` are not known to the author of this plan; if one is found, L7's fixture gains the
  case and the exemption narrows. Fail-closed on parse errors (L8) bounds the damage.
- **Provenance markers are shape-checked pointers**, not store-verified references, and the
  reinstatement *content* is panel-read, not gate-verified (Task 5.3) — stated in the convention
  doc at exactly that strength.

## 11. r1 P2 dispositions (none silently dropped)

| r1 finding | Disposition |
|---|---|
| cold-Opus P2-1 (self-mutation prose > mechanism) | **Folded** — same fix as GROK-P1-1 (§3, L13). |
| cold-Opus P2-2 (row-2 citation off-by-one) | **Folded** — spec row 2 cited as line 681, row 3 as 682 throughout. |
| cold-Opus P2-3 (`GATE_PROJECT` never named) | **Moot on the new surface**; replaced by explicit `--repo` + toplevel-equality refusal and `git -C` discipline (Task 3, AGY-01). |
| cold-Opus P2-4 (Goal prose vs procedural mechanism) | **Folded** — the hedge is in the Goal line itself. |
| cold-Opus P2-5 (run-gate resource limits unexamined) | **Moot** — no `_resource_limiter` on the chosen surface; cited in the §1.1 rejection of option B. |
| cold-Opus P2-6 (Task 7 light-path vs gate machinery) | **Folded** — Task 7 takes the standard path inside this slice's review; light path not used. |
| cold-Opus P2-7 (`/`→`.` id encoding not injective) | **Moot** — the gate's own protocol uses raw repo-relative paths in check ids (§3). |
| grok P2-1 (digest pin doesn't cover `mutation_sweep.py`) | **Folded** — transitive sweep pin, `FAIL[sweep-pin]`, ledger L19. |
| grok P2-2 (L12 enforces presence, prose claims reinstatement) | **Folded** — `kind: reinstate` shape requirement + prose re-stated at enforced strength (Task 5.3, L33). |
| grok P2-3 (support-module edits change fail-ability, ungated) | **Residual-with-trigger** — §8 + §10; targets refused (L13), scope ratchet is an owner decision. |
| grok P2-4 (base ≡ HEAD → vacuous green) | **Folded** for the executed case (L2); wrong-but-resolvable residual named in §10 with the CI trigger. |
| agy AGY-S3P-03 (`test_cmd` interface precision) | **Folded** — direct `run_sweep` import, pre-split `list[str]` (§1.3, Task 4). |

## 12. r2 P2 dispositions (none silently dropped)

| r2 finding | Disposition |
|---|---|
| cold-Opus P2-1 (three mandated mechanisms without ledger rows) | **Folded** — rows added: L31 (repo-root refusal, now also in Task 3 step 1's test list), L32 (`kind` validity), L33 (reinstate presence); §3's completeness claim and §7's Class-V exit key now cover them. |
| cold-Opus P2-2 ("`sys.executable` correct *by construction*" overclaims; L26 is src/-only) | **Folded** — §1.3 restated at mechanism strength ("correct by the canonical invocation and fail-closed otherwise"); the src/-only scope of L26 and the script-by-path case are named as an honest limit in §10 rather than claimed covered. |
| cold-Opus P2-3 (Task 4 startup-order prose contradicts its numbered list) | **Folded** — prose reordered: scoping/declarations before budget, with the reason (count exists only after parse). |
| cold-Opus P2-4 (digest block passes when both sides empty) | **Folded** — non-empty guard line in the canonical block (Task 4; copied into Task 8 manual + CI snippet). |
| cold-Opus P2-5 (`git show "$BASE:…"` unresolvable on fresh `actions/checkout`) | **Folded** — Task 8 step 2 materialises the local ref per `ci.yml.disabled:54-59` before both merge-base and the pin read. |
| cold-Opus P2-6 (rename silently orphans the declaration) | **Folded** — sidecar-must-move rule (§1.2, Task 3), ledger L34. |
| cold-Opus P2-7 (harness-side kill-faking unnamed in §10) | **Folded** — §10 names the engineered-to-red harness case; review of the diff is the control. |
| cold-Opus P2-8 (node-id path form unpinned) | **Folded** — repo-relative paired-file path pinned (§1.3, Task 4 item 4, with `cwd=repo` cited at `mutation_sweep.py:133` and the relative-path precedent at `coverage_mutation_sweep.py:174`); L22's fixture asserts a real declared-id match. |
| sol F4 (digest-check/execution TOCTOU) | **Folded** — §10 names the TOCTOU and the threat-model reasoning at sol's own severity. |
| agy AGY-R2-02 (un-defaulted `$SCRATCH`) | **Folded** — same defect as r2 SOL-F1; canonical block now self-contained (`${SCRATCH:-/tmp/mutation-gate}` + `mkdir -p`, Task 4) and exercised with `SCRATCH` unset (Task 4 step 3). |

## 13. r3 dispositions (none silently dropped)

| r3 finding | Disposition |
|---|---|
| SOL-R3-F1 (P1 — L35 deny-proves only the sweep CLI's handler; the gate CLI's separately mandated handler can regress green) | **Folded** — §1.4 mechanism 2 now enumerates the handler set explicitly and states the enumeration is exhaustive (two entry points, the plan's only `main()`s; `--repin` runs inside the gate's); one deny-proof per handler (L35 sweep CLI / new L36 gate CLI, `test_gate_sigterm_restores_tree_and_exits_nonzero`); Task 4 behaviour item 6 + step 1/step 4 entries; §5 gate bullet names signal handling; Task 6 step 2 inject-reverts each handler independently with the cross-observation (delete one handler ⇒ its row REDs while the other stays green). |
| cold-Opus P1-1 (P1 — tree-shaped `REFUSED_TARGET_TREES` strands any pair whose only killing target is inside a refused tree; demonstrated on `tests/defect_hunts/test_gate_assertions.py`, which Task 6 itself modifies) | **Folded** — cold-Opus's preferred fix adopted: refusal is per-pair, by the paired run's static import surface (paired file + conftest chain + package `__init__` + transitive scoped-tree imports + `pytest_plugins`), §1.2/§3/Task 2/Task 3; anti-bypass property preserved and argued at its actual boundary; both stranded pairs worked as examples with Task 6 executing the defect_hunts declaration for real; L13/L30 rewritten, L40 added (both-ways + probe exclusion); the stranded-pair check **executed** across all five scoped trees (413 files, 49 candidates classified, 3 stranded members) with the found class handled by the declared, gate-verified no-legal-target branch and named in §10. *(r4 note: the executed classification was partially falsified and the declared branch replaced by the owner allowlist — SOL-R4-F1/CO-r4-P2-1, §14; this row records the r3-era disposition as it stood.)* |
| cold-Opus P2-1 (gate-side handler unproved/unlisted) | **Folded** — same fix as SOL-R3-F1 (L36 + Task 4 + §5). |
| cold-Opus P2-2 (§3's "complete enumeration" not literally true: scope-facts, deleted-file exemption, `--repin`, gate handler unrowed) | **Folded** — rows added: L36 (handler), L37 (scope facts always stated), L38 (deleted-file exemption visible), L39 (`--repin` record format); §3's bullet and §7's Class-V key updated to L1–L40. The claim is now literal. *(r4 found four further instances; closed by L41/L42 — §14.)* |
| cold-Opus P2-3 (parametrised/unittest node-id form unpinned; L29's one reason string misattributes id-form errors to the environment) | **Folded** — Task 2 pins the exact collected-form rule (no prefix expansion; the L30 authorbench example is itself unittest class-form, `test_jail.py:4`); L29 split into `NAMED-TEST-NOT-COLLECTED` (id form/typo/rename — names collected near-misses) vs `NAMED-TEST-SKIPPED` (environment — names the DSN remedy), each with its own named test; §1.2/§1.3/§3/§6 restated. |
| cold-Opus P2-4 (`DIRTY-AFTER-SWEEP` conflates untracked residue with a failed restore; `git checkout --` does not work on untracked paths) | **Folded** — §1.4 mechanism 3 and Task 4 item 4 parse porcelain codes: tracked ⇒ `DIRTY-AFTER-SWEEP` + `checkout --`; untracked ⇒ distinct `UNTRACKED-RESIDUE` naming the test run as likely author with `git clean -n`/`-f` guidance (precedent for test-generated residue: `.gitignore:64-67`); operator block updated; L28 gains the second named test. |
| cold-Opus P2-5 (the slice's own diff creates test modules with no authored sidecar — its own merge would FAIL `DECL-MISSING`) | **Folded** — Task 6 step 3 authors sidecars for all three pairs the slice's diff puts in scope (the two new modules plus the modified guard) and runs the gate on the slice branch (`--base dev`, four pairs including Task 7's) as the second live-run subject; §5 lists the three sidecar files. |
| cold-Opus P2-6 (§1.4's "every catchable failure path" slightly stronger than the mechanism) | **Folded** — restated at handler-coverage strength, naming the two catchable-but-uncovered windows (second SIGTERM during restore; signal between `run_sweep` return and the tree check) and routing them to next-touch detection. |

## 14. r4 dispositions (close round — CLOSED per owner go 2026-07-29, sol option 2)

The r4 round was close-or-escalate. SOL-R4-F1 (P1) engaged the third-occurrence
scoping/exemption escalation rail and was escalated to the owner; the owner chose sol's
**option 2** (non-candidate-controlled exemption authority — matching the arc's
retire-self-report direction) and directed the close with the r4 prescriptions applied
("go with recs", 2026-07-29). This v5 amendment is the record-adjudicated close amendment on
the light path: the composition is fully prescribed by the r4 reports plus the owner's option
selection, and it changes plan text only — no gate machinery exists yet to touch.

| r4 finding | Disposition |
|---|---|
| SOL-R4-F1 (P1 — four false legal-target classifications in the executed §1.2 sweep — the deselected implbench placeholders collect zero, `pyproject.toml:89`; and L40 mechanically accepted known false `no_legal_target` claims, the plan's own `test_gate_assertions.py` shape being the counterexample) | **Folded via owner-selected option 2** — exemption authority moved to the owner-reviewed, base-ref-read allowlist (`scripts/mutation_gate_exemptions.json`; §1.2, Task 2, Task 4); a candidate-authored `no_legal_target` sidecar key is `DECL-INVALID` (L40 candidate-authored leg); **collection vitality** added to legal-target classification (the four placeholders — `test_cell_acl_live.py`, `test_cell_runtime_live.py`, `test_process_ledger_live.py`, `test_sandbox_live.py` — fall out automatically; their zero-collection is cited to SOL-R4-F1's executed paired-file evidence); the v4 sweep numbers demoted to author-run estimate with implementor recomputation at build time (§1.2, §10); empty static closures license nothing by themselves (§10 — sol's option-2 rider verbatim); the placeholders' `SCOPED_TEST_TREES` question is routed to the owner at first edit (§1.2). |
| cold-Opus P2-1 (the no-legal-target closure under-specified two ways; each disambiguation broke a v4 claim — reading A strands `test_mcp_sdk_contract`, reading B blinds the check to `src/`) | **Folded** — the closure is precisely defined with **enumerated resolution roots** (repo root, `src/`, `tools/*/` package dirs; CO alternative 2) and conftest-contributed imports in the closure as legal candidates (§1.2, Task 3 — `pair_closure` returns the refused surface AND the legal-candidate set); the `test_mcp_sdk_contract` example is worked both ways: it HAS legal candidates (`tests/arb_memory/conftest.py:15-16` → `src/arb_memory/mcp/`), falls OUT of the no-legal-target population, and its shape (chain conftest importing production) is L40's reject-leg fixture (CO alternative 3); the reading-A fail-closed consequence for that pair is named in §10 as an accepted, loud residual, not papered over. |
| cold-Opus P2-2 (§3's literalness claim still not literal: `EXEMPT-AST` emission, JSON surface/env/duration fields, deterministic pair order unrowed) | **Folded** — L41 (`EXEMPT-AST` emission visible, `test_ast_exempt_pair_emits_visible_pass`); L42 (JSON surface/environment/duration field presence + deterministic sorted-path pair order, `test_json_summary_fields_and_deterministic_pair_order`); §3's bullet and §7's Class-V exit key updated to L1–L42; §4's placement partition updated; Task 3/Task 4 step lists carry the new rows. |
| cold-Opus P2-3 (L35/L36 titled INT/TERM; both deny-proofs exercise TERM only) | **Folded** — both rows retitled to the SIGTERM leg (the leg CPython does not unwind by default); §1.4 mechanism 2 states the SIGINT residual as prose (default disposition raises `KeyboardInterrupt`, the `finally` unwinds regardless — residual nil); Task 1/Task 4 handler text carries the same note. |
| grok r4 (approve, severity none) · agy r4 (non-certifying) | No blocking findings; no amendment items were composed from these reports. |

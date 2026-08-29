# Slice 1e — probe-package artefact: schema, tombstone-surviving re-execution, gated re-landing

**Status: CURRENT (v6) — OWNER-CAPPED 2026-08-01 after five audited rounds.** Author: warm
orchestrator (inline, owner-ruled 2026-08-01; Anthropic-lineage seats non-certifying per
author-non-quorum). Round chain, every verdict audit-closed: r1 four-seat
`panel-slice1e-design-20260801T162122Z-579799` (needs-changes; codex block/P0) → r2
`…-r2-…-a4a9ea` (7/9 CLOSED, 4 new, block/P0) → r3 `…-r3-…-9f2ddb` (3/4 CLOSED, block/P1)
→ r4 `…-r4-…-f678af` (block/P1, nothing-new clean) → r5 `…-r5-…-85094f` (needs-changes/P1:
everything CLOSED except one wording overclaim; class disposition undisputed). v6 fixes
exactly that wording; the owner capped the design at v6 rather than running r6 (B2/B16
precedent — the terminal needs-changes verdict stays on the audit record as the scar).
Every finding→disposition is in §10. The gated-vs-advisory fork was ruled by the owner
2026-08-01: **receipt-required offline gate** (option a). Parent contracts: bus-side gate design §6/§9.2
(`docs/superpowers/specs/2026-07-26-bus-side-gate-design.md:430-457,612-622`), Slice 1
remainder table (`docs/superpowers/plans/2026-07-27-bus-side-gate-slice1d-exempt-lane.md:527`),
co-signed probe-relanding convention (`docs/probe-relanding-convention.md`).

## 1. What 1e is

1. **A typed probe-package schema** — §6's sentence made mechanical: probe source, fixtures,
   seed/harness config, plus the complete run log proving red.
2. **Tombstone-surviving re-execution** — rehydrate `(artefact_id, version)` into a fresh
   disposable worktree at a caller-chosen ref and judge the outcome; the same tool confirms the
   red at `target.commit` and accepts a remediation at its own ref.
3. **A gated re-landing path** — the store-connected deep check (`probe-reland-verify`) emits a
   byte-bound receipt, and the offline mutation gate REQUIRES that receipt for any
   `PROBE_PROVENANCE` pair. Running the deep check before merge is thereby structural, not
   advisory.

Non-goals (contract seams): verifier assignment/family provenance (1f); attestation writes and
harness-author allowlist (1g); close-time re-resolution and spot-check execution (Slice 2);
the claims-row WRITER (unowned here — see §6 for how 1e behaves while the row is unbound).

## 2. The package is ONE artefact

A probe package is a single `application/json` artefact in the existing `artefacts` table
(`src/arb_memory/schema.sql:3-20`). One `(artefact_id, version)` names the whole package; the
domain-separated `content_hash` (`src/arb_memory/hash.py:4`) covers every byte; the existing
fetch path (`src/arb_memory/store.py:288`) retrieves it. Typing is by the `probe_package_v`
schema field. Note the mime participates in the hash preimage (`hash.py:11`), so the publish
seam must actually store `application/json` (§3.1).

### 2.1 Schema (`probe_package_v: 1`)

```json
{
  "probe_package_v": 1,
  "claim_id": "<claim id>",
  "target": {
    "commit": "<40-hex OID the red was demonstrated against>",
    "origin_hint": "<informational free text; never compared>"
  },
  "files": {
    "tests/probe_x.py": {"mode": "create", "content": "<utf-8 source>"},
    "tests/test_existing.py": {"mode": "replace", "content": "<utf-8 source>"},
    "fixtures/seed.json": {"mode": "create", "content": "<utf-8>"}
  },
  "pytest_args": ["-q"],
  "runtime": {"python": "<sys.version at build>", "pytest": "<pytest.__version__ at build>"},
  "red": {
    "expect_failed": ["tests/probe_x.py::test_defect_reproduces"],
    "run_log": "<the COMPLETE red run output>",
    "tree_provenance_stamp": "<the runner's start+OK lines from the red run>",
    "exit_code": 1
  },
  "defect": {
    "path": "src/pkg/mod.py",
    "excerpt": "<bytes that occur verbatim at defect.path in target.commit>"
  },
  "notes": "<free text: what the probe shows, adaptation hints for re-landing>"
}
```

Decisions, with reasons:

- **No free-form invocation, no env (v2; was a panel P0).** The package never carries an argv
  or environment. The rerun tool constructs its own pinned command —
  `.venv/bin/python -m pytest <package test files> --junitxml=<tmp> <pytest_args>` — the same
  pin-your-own-command posture as the merged sweep (`scripts/mutation_sweep.py:145-157`).
  `pytest_args` is validated against a small allowlist (`-q`, `-x`, `-p no:cacheprovider`;
  anything else → `pytest_args_not_allowlisted`). A probe that genuinely needs env or a custom
  runner does not fit v1 and fails loudly at build time (`needs_unsupported_harness`).
- **Runtime is a stated precondition, not a package member (v3; was r2's P0).** The pinned
  interpreter lives at `<checkout>/.venv`, which is gitignored — a fresh worktree does not
  materialize it, and packaging a whole environment would drift. The design's honest position:
  "reproducible by anyone" is narrowed to "reproducible by anyone satisfying the repo's own
  documented test-environment contract" (README § Tests; env-trap B4(g) — the suite ALREADY
  runs only under the checkout venv, so this precondition is repo-standing, not new). The
  rerun tool refuses a missing/unusable venv as a named harness-broken sub-state
  (`rerun_venv_missing`), never a crash; and the package records an informational
  `runtime: {"python": "<version>", "pytest": "<version>"}` block captured at build time so a
  drift-shaped wrong-red has a diagnosis anchor. Repo-level pytest.ini/conftest/plugin state
  at the target ref is part of the pinned tree itself and travels with the commit, not the
  package.
- **Files carry explicit intent** (`mode: create | replace`; was agy's P0). `create` targets
  must not exist at the ref being run; `replace` targets must exist. Intent mismatch at
  materialization is a named refusal either way (`probe_package_create_collision`,
  `probe_package_replace_missing`) — modification probes work, and a package that expects a
  different tree than it meets cannot silently proceed.
- **Path rules are 1e's own validator**, `validate_package_path` — borrowing the *hostility
  posture* of `brief_hydrate.validate_artefact_id` (`src/arb_memory/brief_hydrate.py:42`), not
  the function (which rejects path separators outright and so cannot validate relative paths).
  Rules: relative; no `..`, NUL, absolute, or option-shaped segments; must live under
  `tests/`, `fixtures/`, or be exactly `conftest.py`.
- **`red.expect_failed` is node-id-specific and judged as a SET** (§4).
- **`red.run_log` is complete, never a tail** (was two seats' finding). The parent requires
  "the run log proving red"; truncation is not licensed. Total package content is capped at
  **512 KiB** (`package_too_large`) — an oversize log means quiet the run and re-package, not
  silent truncation.
- **`defect.excerpt` must occur verbatim at `defect.path` in `target.commit`** — verified by
  the build tool at package time and re-verified by the deep check (§5), both via
  `git show <target.commit>:<defect.path>` containment. This binds the anchor to the pinned
  tree instead of to author assertion (was codex's P0). Residual: it proves the bytes existed
  where claimed, not that the probe *exercised* them — stated in §7, Slice 2's spot-check
  territory.

### 2.2 Validator

`src/arb_memory/probe_package.py::validate_probe_package(text) -> RecordCheck`, in the
`faba_schema` idiom (`tools/faba/faba_schema.py:48,317`): accumulate `problems`, never raise;
duplicate-key rejection via `object_pairs_hook`; unknown top-level (and per-section) keys
refused; every §2.1 constraint enforced by shape. The validator gates shape, not truth; the
truth checks that CAN be mechanical (excerpt-in-tree, red actually red) live in the build tool
and deep check, which is why building without the tool is not a supported path.

### 2.3 Trust boundary (stated, not implied)

Rehydrating a package **executes package-supplied code**: test modules and — deliberately
allowlisted — `conftest.py` run at pytest import time. That is the design, and the licence for
it is provenance, not sandboxing: the package arrived via the harness publish gate from an
exempt-lane workspace whose brief is itself a store artefact, and §9.2 of the parent already
licenses executing exempt-origin code *via review*. Operationally: run the rerun tool only in
a disposable worktree (one worktree, one writer), never in a live checkout, and treat the
rerun host as executing that provenance chain's code. A sandboxed rerun profile is a named
residual (§7), not v1 scope.

## 3. Producer path: package before tombstone

`scripts/probe-package-build` (new, exempt-worktree side):

1. Takes `--claim-id`, `--file MODE:PATH` (repeatable), `--defect-path`,
   `--defect-excerpt-file`, `--pytest-args`, and `--expect-failed NODE_ID` (repeatable,
   required — the author DECLARES the intended red; the build-time equality check compares
   the observed failed set against this declaration, which is what keeps it a check rather
   than a tautology; was r2's N4).
2. Verifies `defect.excerpt` occurs in `git show HEAD:<defect.path>` — refusal
   `defect_excerpt_not_in_target` otherwise. (`target.commit` is recorded as the worktree's
   HEAD OID.)
3. Executes the probe under `scripts/tree-provenance-run` using the SAME pinned command shape
   the rerun tool will use, capturing the complete log, exit code, junit, and stamp lines.
   Refuses to package unless the failed set exactly equals `expect_failed`, every named node
   was collected and none skipped, collection was nonzero, and the stamp is OK — the same
   judgment predicate as §4, applied at build time so producer and rerunner cannot diverge.
4. Emits package JSON; validates with `validate_probe_package`.
5. Publishes through the harness gate. **Seam extension (1e-ii, was three seats' finding):**
   `publish_artefact_and_gate` (`tools/faba/faba_launch.py:434`) today reads
   `workspace/artefact.md` and hardcodes `mime:"text/markdown"` (`:472-475,563-570`); 1e adds
   explicit `content_filename` / `mime` / `source` parameters (defaults preserving current
   behaviour) rather than pretending the seam already fits. Never a direct store write.

The exempt lane stays sterile (§6 parent): package travels workspace → harness gate → store;
the worktree dies by tombstone as today.

## 4. Re-execution: `scripts/probe-package-rerun`

`--artefact-id --version [--at <ref>] [--repo <path>]`. `--at` defaults to `target.commit`;
**a remediation is accepted by rerunning at the remediation ref** (was codex's top P0: without
a ref input the tool could only ever observe the defective tree).

1. **Fetch + verify**: `fetch_artefact`; recompute the domain-separated hash against
   `content_hash`; `validate_probe_package`; refusal `probe_package_invalid` on any miss.
2. **Repo rule**: the given repo must contain both `target.commit` and the requested ref
   (`git rev-parse --verify <oid>^{commit}`) — that containment IS the identity check.
   (v1's `repo_identity` field is gone: `canonical_repo_identity` is an absolute local path
   (`bridge.py:2037-2043`) that dies with the tombstone; comparing it would fail every honest
   cross-checkout rerun.)
3. **Materialize** into a fresh disposable worktree at the ref, honouring per-file `mode`
   (§2.1) with **resolved-path containment**: refuse if any ancestor of a target under the
   package roots is a symlink (`probe_package_symlink_ancestor`) and require the final
   resolved path inside the worktree — lexical checks plus O_EXCL on the final component do
   not establish containment (was codex's escape finding; the borrowed atomic-write helper is
   `brief_hydrate.py:76`, not `:132`).
4. **Run** the pinned command (§2.1) under `scripts/tree-provenance-run`, junit to a tmp path
   outside the repo (suite-gate precedent), parsed with defusedxml.
5. **Judge — exhaustive, mutually exclusive, per-node from junit** (was two P0s + two P1s;
   error handling was r2's N3):
   - `rerun_harness_broken` — judged FIRST, before any red/green comparison: zero collection;
     junit missing/unparseable; wrapper stamp not OK; missing venv (`rerun_venv_missing`
     sub-reason); **any junit `<error>` element anywhere in the run** (setup/import/collection
     errors are harness facts, not test outcomes); or any named node whose outcome is anything
     other than plain pass or plain `<failure>` — not collected, skipped, or xfail-reported.
     (The merged sweep refuses the skip/uncollect states by name —
     `mutation_sweep.py:168-176`.) **The xfail family is excluded at the SOURCE** (was r3's
     N3-partial): a NON-strict xpass serializes as a plain pass in pytest junit and is
     therefore undetectable downstream, so the build tool refuses package test files carrying
     xfail markers (`xfail_markers_unsupported`, AST-checked — literal `pytest.mark.xfail`
     decorators AND `pytest.xfail(...)`/`pytest.importorskip` calls and their import-aliased
     forms, best-effort), and both build and rerun pass `-o xfail_strict=true` so any xfail
     that slips in surfaces as a junit-visible failure rather than an invisible pass. Honest
     limit (r4, sharpened r5): AST refusal cannot catch every dynamically-applied marker —
     package-carried application is reviewable (the package test source is in the reland
     diff), but marker application can ALSO originate from repo-level conftest/plugins at the
     pinned target ref, whose source the diff does not carry. The residual is therefore NOT
     mechanically detectable anywhere in 1e. Disposition: a red whose cause is disguised
     behind an evaded xfail is deliberate fabrication (same class as a forged receipt, §5.2
     limit 1); the named backstops are reland review under author-non-quorum for the
     package-carried vector and Slice 2's close-time spot-check design — which this residual
     is explicitly recorded as an input to — for the rest.
   - `rerun_red_reproduced` — no errors anywhere, and the failed set EXACTLY equals
     `expect_failed`. Extra failures are not success: contamination or a different red.
   - `rerun_green` — no errors, every named node ran and passed, zero failures among the
     package's tests.
   - `rerun_wrong_red` — anything remaining (subset red, superset red, different nodes red).
   Distinct exit codes per outcome; the outcome plus stamp lines go to stderr; a rerun-receipt
   JSON (package pointer, ref OID, outcome, failed set, log tail pointer, stamp) to
   stdout/file. `--publish` stores the receipt as an artefact (1g's attestation FK,
   `schema.sql:383-386`, will consume exactly this); the tool never writes attestations.

Acceptance semantics: `rerun_red_reproduced` at `target.commit` confirms the finding;
`rerun_green` at the remediation ref is the acceptance signal — necessary, never sufficient
(§9.5 parity).

## 5. Gated re-landing: deep check + receipt-required offline gate

**Owner-ruled disposition (2026-08-01): option (a).** Two layers:

### 5.1 `scripts/probe-reland-verify` (store-connected)

Given a candidate diff with a `PROBE_PROVENANCE` marker (extracted per
`changed_test_mutation_gate.py:137-145`):

1. Resolve `(probe_artefact_id, probe_artefact_version)`; hash-verify; `validate_probe_package`.
2. `marker.claim_id == package.claim_id` (`reland_claim_mismatch`).
3. **Claims-row triangle** (was codex F7): resolve the `claims` row for `claim_id`; if its
   `probe_artefact_id/version` is non-null it MUST equal the marker pointer
   (`reland_claim_row_mismatch`). A null row pointer is recorded in the receipt as
   `claim_row: "unbound"` — a warning, not a refusal, while the row writer is unowned (§1);
   this tightens to a refusal when the writer lands (1f/Slice 2).
4. Behavioural provenance: every reinstate mutation's `replace` bytes appear verbatim in
   `package.defect.excerpt` (`reland_provenance_mismatch`), AND the excerpt itself re-verifies
   against `git show <target.commit>:<defect.path>` in the repo at hand
   (`reland_excerpt_unverifiable` if the commit is absent — run where the history exists).
5. Name-level containment: the relanded file's test node ids cover `red.expect_failed`
   basenames (adaptation residual stated; convention licenses import/fixture adaptation).
6. **Emit `<testfile>.reland-receipt.json`**: `{reland_receipt_v:1, marker:{claim_id,
   probe_artefact_id, probe_artefact_version}, sha256:{test_file, sidecar,
   package_content_hash}, claim_row: "match"|"unbound", verified_at: "<ref OID>"}` — every
   hash computed from the bytes actually verified.

### 5.2 The offline gate requires the receipt

`changed_test_mutation_gate.py` (sha-pinned; extending it moves the pin deliberately, as the
family expects) adds one rule: a `PROBE_PROVENANCE` pair MUST have a co-committed
`<testfile>.reland-receipt.json` whose `sha256.test_file` and `sha256.sidecar` match the pair's
actual bytes and whose `marker` triple equals the in-file marker — else a named FAIL
(`RECEIPT-MISSING`, `RECEIPT-STALE`, `RECEIPT-MARKER-MISMATCH`). Re-editing the test or
sidecar after verification invalidates the receipt by construction.

**Honest limits (stated at gate strength; second limit was r2's N2):**
1. The offline gate cannot authenticate the receipt against the store — a fabricated receipt
   with self-consistent hashes passes offline. What the gate establishes is that skipping the
   deep check is no longer a silent omission but a deliberate fabrication, exactly the class
   review + Slice 2's spot-check exist to catch.
2. The gate also cannot prove receipt FRESHNESS: a genuine receipt certifies that the deep
   check ran against these exact bytes at `verified_at` — store state (notably the claims-row
   pointer) can change afterwards without touching test, sidecar, marker, or receipt, and no
   one has fabricated anything. This is the same time-varying-state reality that made the
   parent reject offline tokens for close-time decisions
   (`bus-side-gate-design.md:600-610`); freshness authority is Slice 2's close-time
   re-resolution, which re-reads the store. The receipt records the `claim_row` state it saw
   precisely so that re-resolution has something to diff against.
The receipt is an allowlisted-shape artifact, not a signature (no key infrastructure in 1e;
1g's harness-identity work is the natural upgrade path and is cited as scope, not assumed).

## 6. Data model: FK + pairing CHECK

`claims.probe_artefact_id/version` (`schema.sql:346-347`) gains the composite FK to
`artefacts` (mirroring `attestations.rerun_artefact_fk`, `schema.sql:383-386`) **plus**
`CHECK ((probe_artefact_id IS NULL) = (probe_artefact_version IS NULL))` — a composite FK is
`MATCH SIMPLE` in PostgreSQL, so `(id, NULL)` would bypass it (was agy/cold-Opus's finding);
the CHECK closes the half-null dangle. Nullable as today; no reader added in 1e beyond
§5.1(3). Deploy per the B21 pattern (apply as owner role, read back the constraint).

## 7. Residuals (named, not solved)

- **Binary fixtures** — utf-8 only; `files_not_text` refusal. Extension: `files_b64`.
- **Excerpt exercise** — excerpt-in-tree proves presence, not that the probe executed those
  bytes. Slice 2 spot-check territory.
- **Failure-mode fidelity** — set-equality on node ids, not message identity. Messages drift.
- **Receipt authenticity offline** — §5.2's stated limit; upgrade path is 1g.
- **Sandboxed rerun** — §2.3's trust boundary is provenance-based; an isolation profile
  (network-off, resource-limited) is future hardening.
- **`rerun_green` necessary-never-sufficient** — §9.5 parity.
- **Claims-row unbound warning** — tightens to refusal when the row writer exists.

## 8. Testing

Validator red-first per named problem string. Build tool: each refusal branch demonstrated
(green run, VOID stamp, zero collection, excerpt-not-in-target, oversize, non-allowlisted
pytest args, xfail-marker refusal). Rerun: end-to-end in scratch repos — package a red in
worktree A, destroy A, rerun from the store into B at `target.commit` (expect
`rerun_red_reproduced`), apply a fix and rerun `--at` the fix ref (expect `rerun_green`);
**each classifier branch pinned individually** (was r3's new P1): skip, uncollect, junit
missing/unparseable, VOID stamp, missing venv, junit `<error>` element with named nodes
failed (must be harness-broken, not red), junit error with named nodes passed (must be
harness-broken, not green), a strict-xfail demonstration (xfail under
`-o xfail_strict=true` surfaces as failure), and **all three `rerun_wrong_red` shapes**
(subset: one of two named nodes red; superset: named nodes red plus an extra failure;
different-node: red on nodes not named at all — each pinned as `rerun_wrong_red`, never
red-reproduced or green); symlink-ancestor escape attempted and refused. Reland: receipt round-trip green; then each of RECEIPT-MISSING / STALE /
MARKER-MISMATCH demonstrated red-first against the extended gate (pin move included);
provenance mismatch and claim-row mismatch against seeded store rows. Suite results under
`tree-provenance-run`; suite-gate `EXPECTED_MIN_PASSED` moves in the landing commit.

## 9. Slicing the implementation

1. **1e-i** — `probe_package.py` schema + validator (+ `validate_package_path`) + tests.
2. **1e-ii** — publish-seam parameterization (`content_filename`/`mime`/`source`) + tests;
   then `probe-package-build`.
3. **1e-iii** — `probe-package-rerun` (fetch/verify/materialize/judge/receipt).
4. **1e-iv** — `probe-reland-verify` + receipt + mutation-gate extension (pin move) +
   `claims` FK/CHECK migration + convention-doc update.
5. **1e-v** — docs (INDEX, architecture-overview row) + evidence brief.

## 10. v2 changelog — panel finding → disposition

Round `panel-slice1e-design-20260801T162122Z-579799` (codex block/P0, cold-Opus
needs-changes/P0 [non-certifying], agy needs-changes/P0, grok needs-changes/P1):

| Finding (seats) | Disposition in v2 |
|---|---|
| Rerun cannot target a remediation commit (codex F1, P0) | `--at <ref>`; acceptance = `rerun_green` at remediation ref (§4) |
| Gated relanding narrowed to advisory (codex F2 P0; grok P2-5; cold-Opus P1-8) | Owner-ruled receipt-required offline gate (§5.2); honest authenticity limit stated |
| Excerpt authenticates author-controlled values only (codex F3 P0; grok P1-2) | Excerpt must occur in `target.commit` tree; verified at build AND reland (§2.1, §5.1) |
| Unvalidated argv+env execution (cold-Opus P0-1) | Free-form invocation/env removed; rerun pins its own command; `pytest_args` allowlisted (§2.1); trust boundary stated (§2.3) |
| Skipped/uncollected nodes read as green (cold-Opus P0-2; codex F9; grok P1-3) | Exhaustive, mutually exclusive junit-based partition; skip/uncollect → `rerun_harness_broken`; set equality for red (§4.5) |
| Path-collision refusal breaks modification probes (agy F1 P0) | Per-file `mode: create|replace` with intent-mismatch refusals (§2.1) |
| Publish seam hardcodes artefact.md + markdown mime (agy F2; codex F5; cold-Opus P1-5) | Seam parameterization is 1e-ii scope, stated (§3.5); mime-in-preimage noted (§2) |
| Composite FK MATCH SIMPLE half-null dangle (agy F3; cold-Opus P1-6) | Pairing CHECK added (§6) |
| `validate_artefact_id` miscited for paths (agy F4) | Own `validate_package_path`; posture borrowed, function not (§2.1) |
| Symlink-ancestor escape (codex F4) | Resolved-path containment + symlink-ancestor refusal; helper cite fixed to `:76` (§4.3) |
| `repo_identity` is a tombstoned local path (codex F6) | Field dropped; repo-contains-commit rule is the identity check; `origin_hint` informational (§4.2) |
| Claim row can diverge from marker (codex F7; grok P2-4) | Deep check resolves the row; mismatch refuses; unbound warns until a writer exists (§5.1) |
| `run_log_tail` narrows the parent's run log (codex F8; grok P2-3) | Complete log; 512 KiB cap with loud refusal, no truncation (§2.1) |
| conftest.py execution unstated (grok P2-7) | Trust-boundary section (§2.3) |
| Extract-vs-shape and `:132` citation nits (grok P2-1/P2-2) | Citations corrected (§4.3, §5.1) |
| Validates-but-cannot-reproduce underspecification (grok P2-6) | Build-time judgment identical to rerun predicate; no-env/no-invocation removes the main drift axis (§3.3) |

Round r2 `panel-slice1e-design-r2-20260801T170901Z-a4a9ea` (codex fold verification: 7/9
CLOSED, F2/F9 partial, block/P0) — v3 dispositions:

| Finding | Disposition in v3 |
|---|---|
| N1 (P0): no reproducible runtime — pinned .venv gitignored/absent in fresh checkouts | Runtime is a stated precondition (repo's own README § Tests contract, B4(g)); `rerun_venv_missing` named harness-broken sub-state; informational `runtime` block for drift diagnosis; "reproducible by anyone" narrowed honestly with citation (§2.1) |
| N2 (P1, F2 partial survival): genuine receipt outlives store drift | Freshness limit stated as §5.2 honest limit 2, citing the parent's own offline-token rejection; receipt records observed `claim_row` state for Slice 2 to diff (§5.2) |
| N3 (P1, F9 partial survival): junit `<error>` falls through the partition | Harness-broken judged FIRST; any error element → harness-broken; named-node outcomes other than pass/plain-failure (skip, uncollected, xfail, xpass) → harness-broken (§4.5) |
| N4 (P1): builder has no source for `expect_failed` | `--expect-failed` repeatable required flag; author-declared red keeps the equality check non-tautological (§3.1) |

Round r3 `panel-slice1e-design-r3-20260801T171859Z-9f2ddb` (codex: N1/N2/N4 CLOSED, N3
partial, 1 new P1 — block/P1) — v4 dispositions:

| Finding | Disposition in v4 |
|---|---|
| N3 partial: non-strict xpass serializes as plain pass in junit — undetectable downstream | Excluded at source: build-time AST refusal of xfail markers (`xfail_markers_unsupported`) + `-o xfail_strict=true` at build AND rerun so a slipped xfail surfaces as a junit-visible failure (§4.5) |
| New P1: §8 test matrix omits the new classifier branches | Matrix now names every branch individually, incl. error-with-failures ≠ red, error-with-passes ≠ green, venv-missing, strict-xfail demo (§8) |

Round r4 `panel-slice1e-design-r4-20260801T172724Z-f678af` (codex: nothing-new sweep clean;
two survivors — block/P1) — v5 dispositions:

| Finding | Disposition in v5 |
|---|---|
| N3 still partial: dynamic/imported xfail markers evade the AST check; under strict mode an evaded xfail serializes as an ACCEPTED plain failure | AST check extended best-effort to call/alias forms; the irreducible evasion residual is stated as collapsing into the deliberate-fabrication class (§5.2 limit 1) with reland source review + Slice 2 as backstops (§4.5) |
| §8 matrix NOT-CLOSED: rerun_wrong_red branches untested | Subset / superset / different-node shapes each pinned by name (§8) |

Round r5 `panel-slice1e-design-r5-20260801T173427Z-85094f` (codex needs-changes/P1: matrix
CLOSED, nothing-new clean, one wording survivor) — v6 disposition:

| Finding | Disposition in v6 |
|---|---|
| Source-in-diff justification incomplete: repo-level hooks at the pinned ref can apply markers without appearing in the reland diff | §4.5 limit rewritten: package-carried vector reviewable in the diff; repo-level vector not mechanically detectable in 1e; residual explicitly recorded as an input to Slice 2's spot-check design |

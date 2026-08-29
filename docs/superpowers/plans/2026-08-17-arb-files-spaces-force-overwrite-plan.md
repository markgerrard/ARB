# ARB Files Spaces force-overwrite implementation plan

Status: v3 draft after plan-v2 panel remediation
Author: `codex-arbmem-prod`
Worktree: `/home/<user>/arb-worktrees/arb-files-force-fix`
Branch: `fix/arb-files-spaces-force-update`
Base: `4ad7c6002bdd001c56872c71c652fbb972a7f1f1`

Approved specification: `art-9ad659a4b2209b29` v1
Specification approval: `panel-arb-files-force-spec-20260817T021350Z-d58b96`
(`approve/P2`, three independent approvals, reconcile-gated close emitted with zero gaps)

Plan-v1 panel: `panel-arb-files-force-plan-20260817T024620Z-4b89d2`
(`needs-changes/P1`, unanimous convergence, reconcile-gated close emitted with zero gaps)

Plan-v2 panel: `panel-arb-files-force-plan-v2-20260817T061135Z-9c7abe`
(`needs-changes/P1`, two needs-changes/P1 and one approve/P2, reconcile-gated close emitted with
zero gaps). The blocking finding was reproduced by executing Steps 1–2 in a scratchpad: two legacy
tests still pinned the quoted wire value and no action retargeted them, leaving the required GREEN
state at `2 failed, 20 passed`.

## Panel-P2 decisions carried into this plan

- Serialize an existing object's ETag immediately after `head()` and **before** recovery copy or
  audit emission. Serialization failure produces no copy and no audit event.
- Raise `RuntimeError("files backend returned invalid ETag")` for non-string or empty serialized
  values. This permanent representation failure is not labelled retryable.
- The real-Spaces run must capture exactly two local overwrite events: direct with no `via`, then
  presigned with `via="presign"`. It never imports or wires `default_audit_sink`.
- Record every version ETag from a fresh `HeadObject` in its quoted response form. Mutate one opaque
  character inside the quotes, never an outer quote or multipart `-N` suffix.
- Use a host-side `scripts/tree-provenance-run` over the real git worktree to wrap the complete
  tar-to-container and E2E command. `deploy-mcp-1` has neither git nor pytest, so provenance cannot
  originate inside it; the wrapped command copies bytes from the stable worktree and runs the
  dependency-complete production Python.
- Gate metadata uses owner `arb-files` and exact identifiers:
  - interface: `tests/arb_files/test_store_force_race.py::test_force_paths_use_same_unquoted_if_match`
  - backend: `tests/arb_files/e2e_spaces_force.py::test_force_overwrite_real_spaces`
- Update all three Buzz claim sites: top blockquote, measurement table, and capability-gap prose.
  State that quoted-form use is inferred from the measured correct-ETag 412, because Buzz source is
  not in this repository.
- Explicitly defer weak-ETag error differentiation, recurring scheduling, missing-object force
  TOCTOU, guarded recovery copy, and historical audit reconciliation in a tracked note with owner.
- Rollback includes code, tests, docs, layer registry, and load-bearing manifest.

## Step 1 — Establish baseline and red tests

Files:

- `tests/arb_files/test_store_force_race.py`
- `tests/arb_files/test_store_presign.py`
- `tests/arb_files/test_store_crud.py`
- `tests/arb_files/fakes.py`

Actions:

1. Run the focused baseline through provenance:

   ```sh
   scripts/tree-provenance-run .venv/bin/python -m pytest -q -p no:cacheprovider \
     tests/arb_files/test_store_force_race.py \
     tests/arb_files/test_store_presign.py \
     tests/arb_files/test_store_crud.py
   ```

2. Change the fake's conditional comparator to remove at most one surrounding response-header quote
   pair from each side. Keep stored/returned fake ETags quoted. This test-infrastructure change lands
   **before** the production serializer so existing success-path force tests remain meaningful.
3. Add table-driven helper tests for quoted, multipart, unquoted, weak, quoted-empty, empty, and
   non-string values. Include a nested-quote discriminator such as `""abc""` whose expected result
   is `"abc"`; this distinguishes removal of exactly one surrounding pair from `.strip('"')`.
4. Add a combined test named `test_force_paths_use_same_unquoted_if_match` that proves direct
   `IfMatch`, presign `Params["IfMatch"]`, and returned `headers["If-Match"]` all equal `deadbeef`.
5. Add tests that missing/empty ETags raise the exact `RuntimeError` before copy, audit, PUT, or URL
   generation.
6. Explicitly retarget both legacy quoted-wire assertions as part of the RED test change, before
   editing production:
   - `test_force_overwrite_sends_if_match_of_prior_etag` must assert direct `IfMatch == "deadbeef"`;
   - `test_presign_put_force_pins_if_match_to_the_version_it_trashed` must assert both presign wire
     locations equal `"deadbeef"`, while separately retaining the quoted `prior_etag` for recovery
     key and audit identity checks.
7. Name and run every affected behavioral success path that must remain green immediately after the
   semantic fake change, including
   `test_store_crud.py::test_clobber_guard_blocks_overwrite`,
   `test_store_crud.py::test_force_overwrite_trashes_and_audits`,
   `test_store_presign.py::test_presign_put_force_trashes_prior_version_before_issuing_the_url`, and
   `test_store_presign.py::test_presign_put_force_emits_an_overwrite_audit_event_marked_as_presigned`.
8. Run the complete focused set. Require the four behavioral paths in item 7 to remain GREEN under
   the semantic fake. Record expected RED only for the missing helper/new helper tests, the combined
   unquoted-wire test, and the two explicitly retargeted legacy wire pins in item 6. A stale-write
   failure in a behavioral path is a plan/test defect and must be fixed before production changes;
   it is not evidence against the production fix.

## Step 2 — Implement the minimal production fix

File: `src/arb_files/store.py`

Actions:

1. Add `_put_if_match_etag(value: object) -> str` with the approved one-pair algorithm and exact
   `RuntimeError` above.
2. In each force path, after `head()` reports `exists`, compute the serialized ETag before
   `copy_object` and `_emit_audit`.
3. Preserve the existing recovery copy and audit ordering relative to the conditional PUT after
   successful serialization.
4. Attach the computed value unconditionally on the existing-object branch; remove both truthiness
   gates.
5. Do not change missing-object force, create-only, trash derivation, 412 mapping, audit schema, or
   any other storage operation.
6. Run the complete Step-1 focused set, including the four behavioral paths and both retargeted
   legacy wire-pin tests, through `tree-provenance-run`; require GREEN and an `OK` stamp.

## Step 3 — Confirm the semantic fake did not erase wire pins

File: `tests/arb_files/fakes.py`

Actions:

1. Inspect the Step-1 comparator and confirm it is used only for backend conditional matching.
2. Confirm stored and returned fake ETags remain quoted.
3. Re-run the exact wire-value tests with a deliberate temporary quoted-value mutation and require
   RED, then restore the production implementation and require GREEN. This rival-instrument check
   proves semantic comparison did not dissolve the call-shape assertion.
4. Re-run all `tests/arb_files` unit/interface tests through provenance.

## Step 4 — Add the checked-in real-Spaces harness

Files:

- `tests/arb_files/e2e_spaces_force.py` (new, importable implementation, exact test function, and
  `main()`)
- `tests/arb_files/e2e_local_mcp.py` (reuse or share paginated raw cleanup helpers)

Actions:

1. Implement the ordered create → direct force → presigned force → stale replay → wrong-token
   sequence from the spec.
2. Fetch `etag_v1`, `etag_v2`, and `etag_v3` through fresh heads and retain their quoted forms in
   the transcript.
3. Derive the wrong token from the opaque body, preserving quotes and any multipart suffix, pass it
   through `_put_if_match_etag`, and require 412.
4. Verify bytes after every success and every rejected write.
5. Capture available request IDs and statuses without recording credentials or URLs.
6. Assert exactly two local audit events with the expected `op`, `via`, and recovery keys; do not
   import `arb_files.audit.default_audit_sink`.
7. In `finally`, paginate and raw-delete every UUID-bearing live/trash key, then separately paginate
   and assert zero residue. Emit the JSON transcript only after cleanup outcome is known.
8. Define `test_force_overwrite_real_spaces()` in this module as the one backend-preserving test.
   It skips unless `ARB_FILES_E2E=1`, using a lazy pytest import only on the disabled pytest path so
   the enabled production-container path has no pytest dependency. `main()` checks the same gate and
   invokes that exact function, so the load-bearing identifier names the code executed inside the
   pytest-free production container rather than an unexecuted wrapper. A skipped collection or
   disabled `main()` run is explicitly not backend evidence.

## Step 5 — Reconcile documentation and track deferred work

Files:

- `deploy/buzz/README.md`
- `docs/superpowers/notes/2026-08-17-arb-files-force-followups.md` (new)
- `docs/index.json`

Actions:

1. Correct the README top warning, table row, and capability-gap paragraph. Preserve the conclusion
   that Buzz must not use Spaces: only sequential unquoted ARB Files CAS is proven; its 32-writer
   gate remains untested in that representation.
2. Label quoted-form use as an inference from the recorded correct-ETag 412, not a source-inspected
   fact.
3. Record owned follow-ups for periodic parser-drift scheduling, weak-ETag error differentiation,
   missing-object force TOCTOU, `CopySourceIfMatch`/audit integrity, and historic-event
   reconciliation. Owner: `arb-files`; status: deferred after this hotfix.
4. Register the follow-up note in `docs/index.json` with a single-line purpose of at most 120
   characters that tersely names all five deferred obligations. Run `scripts/gen-doc-index` to
   regenerate `docs/INDEX.md`, then require `scripts/check-doc-index` to pass.
5. Record that the exact 82-byte v1 probe trash object was raw-deleted and directly re-headed as
   absent; no second credential-bearing vantage was available without moving production secrets.

## Step 6 — Add load-bearing gate metadata

Files:

- `skills/bridge-protocol/gate/layer_registry.json`
- `skills/bridge-protocol/gate/load_bearing_components.json`

Actions:

1. Add pattern `src/arb_files/**`, layer `storage-adapter`, owner `arb-files`, required dimensions
   `interface` and `backend`.
2. Add `src/arb_files/store.py` with both dimensions costly, the exact test IDs named above, and
   evidence text distinguishing interface pins from the live Spaces property.
3. Permit fakes only for serializer/request-shape checks, not backend certification.
4. The backend evidence string must be
   `tests/arb_files/e2e_spaces_force.py::test_force_overwrite_real_spaces`; `main()` executes that
   exact function in the production container and an explicit pytest invocation can collect the
   same node where pytest is available.
5. Validate both JSON files and run the bridge gate tests through provenance.

## Step 7 — Run unit and gate verification

Commands, each through `scripts/tree-provenance-run`:

```sh
.venv/bin/python -m pytest -q -p no:cacheprovider tests/arb_files
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_bridge_protocol_gate.py
```

Then run `git diff --check` and inspect the complete diff against the approved spec and panel-P2
decisions.

## Step 8 — Run the production-container E2E with host-tree provenance

Wrap a single shell command with host `scripts/tree-provenance-run`. Inside that command:

1. create a unique, exact `/tmp/arb-files-force-e2e-<nonce>` directory in `deploy-mcp-1`;
2. tar the current worktree's `src/arb_files`, `tests/arb_files`, and necessary package markers into
   it without copying credentials;
3. execute from that directory with `PYTHONPATH=src ARB_FILES_E2E=1 python -m
   tests.arb_files.e2e_spaces_force`;
4. retain the complete JSON transcript and exit status; and
5. remove the exact container scratch directory in a trap.

The host provenance wrapper must emit `tree-provenance: OK` and the E2E must exit zero. Independently
confirm the transcript says both negative cells were 412, both success cells were 2xx, final bytes
matched, exactly two local audit events were captured, and cleanup residue is empty.

## Step 9 — Tri-review and merge gate

1. Publish the implementation diff and verification transcript as panel briefs.
2. Run the pinned Grok, Pi/GLM, and cold-Opus implementation tri-review independently.
3. Resolve every P0/P1 and re-run affected tests plus the rival-instrument probe after remediation.
4. Close the audited tri-review with zero reconciliation gaps.
5. Produce bridge-protocol phase inputs for design, spec, plan, and implementation using
   `manual-panel`; run the executable gate and retain `gate_result.json`.
6. Commit only after the gate passes. Do not push, merge, or deploy unless separately requested.

## Rollback plan

Revert `store.py`, its tests/harness, Buzz documentation, the follow-up note, the layer-registry rule,
and the load-bearing manifest entry together. Re-run the gate after rollback. No data/schema rollback
exists. Force overwrite returns to the incident's known 412 behavior and recoverable workaround.

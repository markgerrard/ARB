# Open Interpreter vs Pi isolated bakeoff — executable TDD implementation plan

> **Execution contract:** implement Tasks 1–13 task-by-task with a Luna-high Codex bridge seat in
> isolated worktrees. Every Luna task starts with named failing tests, goes green locally, commits only
> its declared files, and returns SHA + test counts. Do not improvise around a frozen-spec conflict.

## Sources of truth and phase wall

- Frozen design: `docs/superpowers/specs/2026-07-13-openinterpreter-pi-isolated-bakeoff-design.md`
  at commit `166eb76bcf50ea28d44bfccf3604b8c824bf698a`, blob
  `c5981bcdc3bf4d52a5cdfaff58a26df336771fb7`.
- Frozen implementation spec: reviewed revision
  `614579031700af95111493b563f7d7bb39065aff`, blob
  `9fbf8bacf045395ec00ca55ba8f5a1e3479f6ad9`; convergence audit
  `panel-oi-pi-impl-spec-r27-20260714T072639Z-0ead32`, unanimous approve, emitted/no gaps.
- This plan translates those documents; it does not respec. A contradiction stops the affected task
  and is recorded under **Escalations**. It is never resolved by weakening isolation, evidence, or
  classification.
- No calibration, pilot, or scored cell may run during Tasks 1–13. Task 14 is readiness only.
  Calibration/pilot/full execution happens after implementation review reaches audited zero P0/P1.
- Credentials previously pasted into chat must be rotated before Task 14 live preflight. Never place
  credentials in Git, argv, a task worktree, reports, or tool-process environments.

## Baseline and worker protocol

Before Task 1, from a clean orchestration checkout:

```bash
git diff --check
.venv/bin/python -m pytest -q
PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests
scripts/check-doc-recipes
scripts/check-doc-drift
```

Record exact failures as baseline; new failures are not inherited. Each Luna task uses a fresh
worktree name `oi-bakeoff-tNN-<nonce>` based on the current integration HEAD and a dedicated branch.
The worker may read frozen design/spec/plan and declared dependency interfaces, but must not edit the
orchestration checkout. Integration is sequential in the dependency order below; before cherry-pick,
the orchestrator verifies scope, tests, clean status, and commit. After cherry-pick, rerun the task's
tests. Never merge a task with uncommitted work, skipped deny-proofs, or an unresolved escalation.

Common test conventions:

- Hermetic unit/integration tests use temporary roots, fake engines/providers/Redis, descriptor-safe
  fixtures, and subprocess stubs; they must run without live credentials.
- Boundary tests that need macOS ACL/Seatbelt/UIDs or managed Valkey are marked `live_bakeoff` and
  run only in Task 14 through the production readiness command. A skipped live test is not readiness
  evidence.
- Every security mechanism lands with a negative test that is RED when the mechanism is removed.
- Canonical JSON parsers reject duplicate/unknown fields, non-canonical numbers, unknown versions,
  oversized frames, and trailing bytes.

## Dependency and merge map

```text
T1 Open Interpreter engine ─────────────────────────────────────────────┐
T2 immutable schemas + schedule ─┬─ T3 append-only evidence/auth ──────┤
                                 └─ T4 sandbox profiles/launch ────────┤
T3 + T4 ─> T5 cell lifecycle/ACL/process journal ──────────────────────┤
T2 + T3 + T5 ─> T6 Git service/receipts/completion ────────────────────┤
T6 + T5 ─> T7 quarantine export/importer/ref protection/prune core ────┤
T3 + T4 + T7 ─> T8 scorer sandboxes + G0-G7 classifier ───────────────┤
T1..T8 ─> T9 controller close/recovery ────────────────────────────────┤
T1..T9 ─> T10 seat + aggregate readiness gates ───────────────────────┤
T2,T3,T7,T8,T9,T10 ─> T11 CLI/evidence/report/prune surface ──────────┤
T9..T11 ─> T12 calibration/pilot/matrix phase machinery ──────────────┤
T1..T12 ─> T13 adversarial/integration suite + docs ──────────────────┤
T13 ─> T14 live readiness evidence (no scored cells) ─────────────────┘
```

Tasks are integrated in numeric order. A task may be split into multiple commits only where its
commit shapes say so; downstream worktrees always branch from the latest integrated dependency HEAD.

## Task 1 — Open Interpreter 0.0.21 engine and dispatcher registration

**Files:** new `src/agent_redis_bridge/engines/openinterpreter.py`; edit
`src/agent_redis_bridge/engines/__init__.py`, `src/agent_redis_bridge/bridge.py`,
`src/agent_redis_bridge/ctl.py`,
`scripts/agent-dispatch`, `scripts/implbench`, `pyproject.toml`; new
`tests/test_openinterpreter_engine.py`; extend `tests/test_bridge_control_containment.py`,
`tests/test_bridge_engine_failure_containment.py`, `tests/test_agent_dispatch.py`,
`tests/test_bridge.py`, `tests/test_bridge_identity.py`, `tests/test_bridge_notify_inbox.py`,
`tests/test_bridge_parallelism.py`, `tests/test_bridge_turn_heartbeat.py`,
`tests/test_bridge_handle_raw.py`, `tests/test_ctl_worktree.py`, `tests/test_fifo_order.py`,
`tests/test_agent_dispatch_audit_panel.py`, `tests/test_agent_dispatch_queue.py`, and
`tests/test_agent_dispatch_run_id.py` where engine-choice expectations change.

- [ ] RED: protocol success/error/malformed/oversized frame; missing explicit provider/model/harness;
  exact `interpreter app-server --listen stdio://` argv without shell; tool request routed only to
  injected broker; direct shell/file/code execution denied; terminal error surfaced; independent
  control acknowledgement/provenance; fresh context; retire-after-turn; timeout/kill/reap.
- [ ] Add optional `bench` dependency group with `PyYAML`; do **not** install
  `open-interpreter==0.0.21` into the ARB environment because its `openai<0.28` requirement conflicts
  with ARB's `arb-memory` `openai>=1.0`. Treat Open Interpreter as the separately installed scored
  executable: require an explicit absolute `OI_INTERPRETER_BIN`, reject PATH lookup/symlinks, verify
  its realpath, `interpreter --version == 0.0.21`, executable SHA-256, and manifest pin before launch.
  Implement `scripts/implbench` deterministic repo-Python environment selection with no system-Python
  fallback. RED executes it under a PATH whose `python3` lacks PyYAML and proves it still selects the
  synced repo environment; missing bench extra or missing/mismatched OI executable fails loudly.
- [ ] Register the `live_bakeoff` pytest marker and default
  `addopts = -m 'not live_bakeoff'` in `pyproject.toml`; a RED sentinel marked `live_bakeoff` must
  not execute in any hermetic GREEN/full-suite command. T14 overrides with `-m live_bakeoff` and
  proves the sentinel does execute there.
- [ ] Implement bounded app-server adapter with `BRIDGE_INTERPRETER_RETIRE_AFTER_TURN=1`, explicit
  `zcode|kimi-cli`, structured provenance, and no continuation/warm-thread path.
- [ ] Register `openinterpreter -> interpreter` in every engine, CLI, status, and dispatch table;
  extend engine containment tests.
- [ ] GREEN:
  `env -u IMPLBENCH_BATTERY_KEY .venv/bin/python -m pytest -q tests/test_openinterpreter_engine.py tests/test_bridge_control_containment.py tests/test_bridge_engine_failure_containment.py tests/test_agent_dispatch.py tests/test_bridge.py tests/test_bridge_identity.py tests/test_bridge_notify_inbox.py tests/test_bridge_parallelism.py tests/test_bridge_turn_heartbeat.py tests/test_bridge_handle_raw.py tests/test_ctl_worktree.py tests/test_fifo_order.py tests/test_agent_dispatch_audit_panel.py tests/test_agent_dispatch_queue.py tests/test_agent_dispatch_run_id.py`.

**Done:** deleting broker interception or retirement makes a named test fail; `uv sync --extra bench`
from a fresh clone followed by `OI_INTERPRETER_BIN=<absolute-pinned-0.0.21-binary>
scripts/implbench --help` succeeds without resolving the incompatible OI Python package into ARB.

**Commit:** `feat(engine): add isolated Open Interpreter 0.0.21 adapter`.

## Task 2 — manifest-v2, authoritative schemas, exact corpus and schedule

**Files:** new `bench/implbench/harness/manifest.py`, `bench/implbench/harness/schedule.py`; edit
`bench/implbench/harness/tasks.py`;
new `bench/implbench/tests/test_manifest_v2.py`, `bench/implbench/tests/test_schedule_v2.py`;
extend `bench/implbench/tests/test_fixtures.py` regression expectations.

- [ ] RED manifest tests for exact frozen design/spec/plan commit+blob pins, canonical source
  realpath, source/base/corpus/fixture SHAs, exact four arms/provider/model/harness/
  agent-prefix/home/retire fields, exact eight tasks and fixture SHAs, all requested/effective/
  verified-via controls, exact `medium`, binary/profile/importer/scorer/battery pins, RPC constants,
  budgets, digest versions, extension empty allowlists, evidence root, stop/rerun/analysis rules.
- [ ] RED canonical-schema tests: missing/unknown/duplicate fields, duplicate JSON keys, unknown
  schema/version/enums, non-canonical numbers, secret/credential path, dirty/symlinked source,
  realpath/SHA drift, mutable manifest, and active-checkout env source all fail closed.
- [ ] RED schedule vectors start from task IDs sorted by UTF-8 bytes and use descending Fisher-Yates
  with the specified SHA-256 first-word rejection sampler, including a forced rejected word that
  advances counter. Require a 32-byte seed encoded as exactly 64 lowercase hex characters;
  zero-based task index; odd/even arm order; pair order;
  even-repetition reversal; exact `rep→pair→task→arm` nesting; 128 unique schedule indices/cell IDs.
- [ ] Implement frozen dataclasses/validators, atomic mode-0600 manifest creation and revalidation,
  exact corpus loader, cell suffix derivation, and independent schedule expansion.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_manifest_v2.py bench/implbench/tests/test_schedule_v2.py bench/implbench/tests/test_fixtures.py`.

**Done:** golden manifest and schedule round-trip byte-identically; changing one pin or nesting fails.

**Commit:** `feat(implbench): freeze manifest-v2 schemas and 128-cell schedule`.

## Task 3 — append-only canonical evidence, authentication, and sealed records

**Files:** new `bench/implbench/harness/records.py`, `bench/implbench/harness/authlog.py`;
replace/extend `bench/implbench/harness/evidence.py`; new
`bench/implbench/tests/test_records_v2.py`, `bench/implbench/tests/test_authlog.py`; extend
`bench/implbench/tests/test_validate.py` regression expectations.

- [ ] RED tests for the mandatory identity envelope, prior-record digest chain, fsync-before-ack,
  truncation/replacement/replay/sequence/nonce detection, schema versioning, bounded quarantine,
  public value-free projection, and restart verification.
- [ ] RED schema tests for Git receipts, budget records and split authorship, G4 receipts, completion
  payload/envelope, pre-scorer input attestation, post-G4 receipt attestation, repair authorization,
  private census + public digest, gate records, telemetry, provenance, bounded unavailable status,
  and all closed enums.
- [ ] Implement canonical JSON byte serialization and append-only NDJSON writer using descriptor
  ownership, `O_NOFOLLOW`, file/dir fsync, monotonic controller sequence, per-attempt MAC/nonce, and
  public/private evidence separation.
- [ ] Preserve a tested ordinary-v1 `Recorder.write(record)` compatibility adapter for existing
  non-bakeoff CLI/report consumers until T11 migrates them. It refuses any
  `oi-pi-bakeoff-*` run ID or manifest-v2/scored record, so no scored path can bypass the envelope.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_records_v2.py bench/implbench/tests/test_authlog.py bench/implbench/tests/test_validate.py bench/implbench/tests/test_cli.py bench/implbench/tests/test_report.py`.

**Done:** every mutation/replay fixture fails before a consumer can classify or score.

**Commit:** `feat(implbench): add authenticated append-only evidence contracts`.

## Task 4 — generated Seatbelt profiles and verified launch boundary

**Files:** new `bench/implbench/harness/sandbox.py`; profile templates under
`bench/implbench/profiles/`; new `bench/implbench/tests/test_sandbox_profiles.py`,
`bench/implbench/tests/test_sandbox_launch.py`, and
`bench/implbench/tests/test_sandbox_live.py` marked `live_bakeoff` for real Seatbelt/Mach/profile
launch and network/filesystem deny proofs used by Gates 9–11.

- [ ] RED static profile tests for controller/provider+bus-only egress, tool no-network including
  loopback, Git-service no-network, evidence/base/sibling/credential/key denial, fixed Mach allowlist,
  no broad filesystem/user-group grants, and pinned profile/Mach digests.
- [ ] RED hermetic launch-construction tests (stub the OS launcher, never skip) for scrubbed
  allowlisted env, no Git binary/config/metadata in control/tool,
  no role/project/skill/MCP/extension surface, fresh home/context/process, no resume/fork/warm path,
  `PYTHONDONTWRITEBYTECODE=1`, exact UID and root ownership, and fail-closed profile mismatch.
- [ ] Implement profile generator, descriptor/inherited-secret mount contracts, launch verifier, and
  role instrumentation needed for later bytecode attribution. Mark separate real Seatbelt/UID launch
  probes in `test_sandbox_live.py` and bind them explicitly to T14/Gates 9–11; they are not counted
  by T4 GREEN.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_sandbox_profiles.py bench/implbench/tests/test_sandbox_launch.py`.

**Done:** each forbidden endpoint/path/extension has a non-skipped static and launch-construction
negative proof. Real launched proof remains an explicit T14 readiness obligation and cannot be
claimed here.

**Commit:** `feat(implbench): add pinned three-plane sandbox profiles`.

## Task 5 — disposable cell, UID/process ledger, Redis ACL lifecycle

**Files:** new `bench/implbench/harness/cell_runtime.py`; new
`bench/implbench/tests/test_cell_runtime.py`, `bench/implbench/tests/test_cell_acl.py`,
`bench/implbench/tests/test_process_ledger.py`, plus `live_bakeoff` modules
`bench/implbench/tests/test_cell_runtime_live.py`, `bench/implbench/tests/test_cell_acl_live.py`, and
`bench/implbench/tests/test_process_ledger_live.py` for real ephemeral UIDs, double-fork census,
POSIX ACLs, and managed Valkey user/prefix/client lifecycle used by Gates 9, 13, and 14.

- [ ] RED tests for `/Users/Shared/arb-implbench/<run-id>` canonical roots, controller ownership,
  narrow POSIX ACLs, fresh control/tool/Git UIDs, homes/configs/bus namespaces, write-ahead journal,
  crash recovery after every provisioning side effect, and descriptor-safe deletion.
- [ ] RED Redis tests with fake protocol for random per-cell user/prefix, deny cross-prefix, pre/post
  emptiness, disable→kill clients→delete prefix/user order, retired auth failure, not-provisioned
  cleanup probes, and never treating endpoint reachability as namespace proof.
- [ ] RED process tests for double-fork/`setsid` descendants, complete session ledger, SIGTERM grace,
  SIGKILL, independent UID census, and absence proofs on every exit.
- [ ] Implement lifecycle APIs only; no importer/scorer/classification yet.
- [ ] RED live modules fail against a deliberately unretired UID/client, cross-prefix access,
  retained ACL/root, or retired credential that still authenticates. They run only in T14.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_cell_runtime.py bench/implbench/tests/test_cell_acl.py bench/implbench/tests/test_process_ledger.py`.

**Done:** fault injection at every journal phase converges to absent UID/process/ACL/root state.

**Commit:** `feat(implbench): add journaled disposable-cell lifecycle`.

## Task 6 — bounded Git RPC service, receipts, completion sealing

**Files:** new `bench/implbench/harness/git_service.py`,
`bench/implbench/harness/receipts.py`, `bench/implbench/harness/completion.py`; new
`bench/implbench/bin/git-shim`; new `bench/implbench/tests/test_git_service.py`,
`bench/implbench/tests/test_receipts_v2.py`, `bench/implbench/tests/test_completion_v2.py`.

- [ ] RED framing tests for four-byte big-endian length, exact constants, incremental canonical JSON,
  bounded in-flight/rate/status, traversal/path/component rejection, and tool/ingress controller-only
  versus service `status|hash|stage|tree|commit` budget authorship.
- [ ] RED service tests for closed status/add/commit only, controller-assigned sequence/MAC/fsync,
  first-parent fixture/prior-receipt chain, allowlisted changed paths, pinned tree digest, no hooks/
  config/helpers/filters/attributes/alternates/replacements/SSH/fsmonitor, hardlink staging rejection,
  and no host Git bypass.
- [ ] RED completion tests for exact empty/dirty/non-empty truth tables, final status unavailable
  without throw, whole-worktree no-follow materialization digest, symlink/TOCTOU/version mismatch,
  authenticated seal, and post-import replay/pin/digest rejection using the frozen G4 receipt schema
  already delivered by T3 (the G4 execution producer remains T8).
- [ ] Implement service/shim and seal APIs with Git-service ceilings <= importer ceilings.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_git_service.py bench/implbench/tests/test_receipts_v2.py bench/implbench/tests/test_completion_v2.py`.

**Done:** removing the shim/service, widening any RPC/service ceiling, weakening a seal predicate, or
re-enabling a forbidden Git surface makes a named hostile test fail. Bridge scored-path legacy
completion selection is implemented and tested in T9, not assumed here.

**Commit:** `feat(implbench): add bounded Git service and completion receipts`.

## Task 7 — quarantine export, complete census, descriptor-held importer

**Files:** new `bench/implbench/harness/quarantine.py`,
`bench/implbench/harness/importer.py`, `bench/implbench/harness/ref_protection.py`; extend
`bench/implbench/harness/fixtures.py`; new `bench/implbench/tests/test_quarantine.py`,
`bench/implbench/tests/test_importer_v2.py`, `bench/implbench/tests/test_census.py`,
`bench/implbench/tests/test_ref_protection.py`; extend `bench/implbench/tests/test_fixtures.py` for
the new cell/attempt ref-helper signatures and legacy-shape rejection.

- [ ] RED export tests for exact base+fixture refs, `files` backend, no-local/no-tags exact refspec,
  explicit `reftable` rejection, full ref/ODB census, prior-result ref and dangling-object poisons,
  cell-only poisons, canonical
  private census digest, and no clone transfer/dispatch on failure.
- [ ] RED importer attacks for symlink/hardlink/device/FIFO, inode/type/size/hash races, malformed
  loose/pack/index, bombs/ratio/depth/count/bytes/fd/time limits, unknown objects, bad parent/tree/path,
  gitlinks/escaping links, config/env injection, and source-path reopen.
- [ ] Implement descriptor-held one-pass copy to importer-owned spool, incremental limits before
  allocation/write, object hash/reconstruction, strict fsck, independent tree diff/allowlist,
  receipt/digest reproduction, content-addressed bundle, and spool destruction on failure.
- [ ] RED/implement the exact `oi-pi-bakeoff-` run-ID parser, canonical
  `refs/implbench/{runs|results}/<run_id>/<cell_id>/<attempt_id>` bakeoff APIs, legacy task-only
  rejection on every bakeoff API,
  evidence-root protection union, mandatory-root refusal, and date-prune core. Prove canaries (a)
  final-index-only protected ordinary ref, (b) prefix-only protected bakeoff ref, (c) active
  manifest/journal-only protected ref, and (d) missing/invalid root refusal, while an eligible
  ordinary ref and only that ref is deleted and protected reachable objects stay byte-identical.
- [ ] Preserve explicitly named ordinary-v1 ref/prune adapters for the current non-bakeoff
  `dispatch.py`/`cli.py` only; they hard-refuse `oi-pi-bakeoff-*`, manifest-v2, cell, or attempt
  inputs. T9 removes run/result adapters after dispatch migration; T11 removes the no-root prune
  adapter after CLI migration. Tests prove neither adapter is callable from a scored path.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_quarantine.py bench/implbench/tests/test_importer_v2.py bench/implbench/tests/test_census.py bench/implbench/tests/test_ref_protection.py bench/implbench/tests/test_fixtures.py bench/implbench/tests/test_cli.py`.

**Done:** consistent-seal importer faults produce typed infrastructure errors, never non-delivery;
all four ref-protection canaries and ordinary deletion run in the GREEN command.

**Commit:** `feat(implbench): add quarantined descriptor-held importer`.

## Task 8 — isolated G1/G4 scoring and frozen classifier

**Files:** new `bench/implbench/harness/scorer_sandbox.py`,
`bench/implbench/harness/classifier.py`; refactor `bench/implbench/harness/scoring.py`,
`bench/implbench/harness/battery.py`,
`bench/implbench/harness/gates.py`, `bench/implbench/harness/validate.py`; new
`bench/implbench/tests/test_scorer_sandbox.py`,
`bench/implbench/tests/test_classifier_v2.py`, `bench/implbench/tests/test_bytecode_attribution.py`;
extend `bench/implbench/tests/test_validate.py`, `bench/implbench/tests/test_gates.py`,
`bench/implbench/tests/test_scoring.py`, and
`bench/implbench/tests/test_battery.py` to remove legacy
host-completion/G2/G4 assumptions.

- [ ] RED G1 tests for keyed-runner/broker/submitted-program UIDs, controller-only inherited
  `IMPLBENCH_BATTERY_KEY`, no plaintext/key/output exfiltration, bounded model code, and UID reap.
- [ ] Remove/replace legacy `battery.run_battery` host decryption/execution: production hidden
  batteries run only through the keyed-runner topology. A monkeypatch tripwire fails if host Python
  decrypts or executes submitted code.
- [ ] RED G4 tests for keyless coordinator/suite-runner-broker/submitted-code UIDs, pinned public
  suite, and every receipted OID. Imported earlier-FAIL/later-PASS receipts demonstrate TDD; fewer or
  no qualifying receipts on a successfully imported non-empty delivered attempt yield
  `NOT_DEMONSTRATED`, never correctness FAIL. Empty/not-delivered attempts never import or score and
  set G4 `NOT_SCORED`; infrastructure cascade sets G4 `UNKNOWN`.
- [ ] RED table-driven G0-G7 precedence for ordinary, empty, dirty, budget, auth/version/verifier,
  importer/attestation, scorer supervisor, execution timeout, and overlapping failures.
- [ ] RED closed `failure_category` tests: model-authored failure is
  `model-implementation`; malformed/truncated/version- or identity-mismatched boundary frames and
  receipt/import/integrity/attestation failures are `protocol-import-infrastructure`; all other or
  ambiguous infrastructure is `other-infrastructure`. Protocol/import and other infrastructure
  force `UNKNOWN` and can never be counted as implementation losses.
- [ ] RED scorer-input provenance: G1/G3/G5–G7 accept only the trusted post-import materialization
  descriptor/digest. Any live-cell path, pre-import tree, fixture-tip surrogate, or controller-side
  working tree fails closed before scorer launch. Public G2 is exactly
  `agent-delivered|not-delivered`, plus `UNKNOWN` only for the authoritative cascade.
- [ ] RED adversarial validator migration: receipt/seal-based G2 priors, frozen public G2 labels,
  imported non-empty G4 `NOT_DEMONSTRATED`, empty/not-delivered G4 `NOT_SCORED`, and infrastructure
  `UNKNOWN`; any legacy `DELIVERED|RESCUED|NOT-DELIVERED|NOT-DEMONSTRATED` public input trips the
  validator rather than being normalized.
- [ ] RED six-role whole-sandbox bytecode tests: imported/model-created => G5; infrastructure role =>
  UNKNOWN; unprovable attribution => UNKNOWN; any `.pyc`/`__pycache__` anywhere fails closed.
- [ ] RED scorer-output quarantine tests: each role writes hostile stdout/stderr/assertion/traceback
  canaries only to an unlinked descriptor-only sandbox sink; the controller has no readable
  descriptor or pathname; a trusted in-sandbox encoder returns only the authenticated fixed schema;
  every sink disappears after success, failure, timeout, and cancellation. Do not add a seventh role.
- [ ] Preserve a tested ordinary-v1 `evaluate_gate` compatibility path solely for the current
  non-bakeoff `dispatch.py` and `validate.py`; it hard-refuses manifest-v2/scored inputs and legacy
  public labels never enter new evidence. T9 deletes this compatibility path when it migrates both
  consumers.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_scorer_sandbox.py bench/implbench/tests/test_classifier_v2.py bench/implbench/tests/test_bytecode_attribution.py bench/implbench/tests/test_gates.py bench/implbench/tests/test_scoring.py bench/implbench/tests/test_battery.py bench/implbench/tests/test_validate.py bench/implbench/tests/test_cli.py`.

**Done:** exhaustive decision table has one authoritative outcome per overlap and zero secret leaks.

**Commit:** `feat(implbench): isolate scorers and freeze G0-G7 classifier`.

## Task 9 — production controller close state machine and crash recovery

**Files:** new `bench/implbench/harness/controller.py`; complete
`bench/implbench/harness/cell_runtime.py`; replace orchestration in
`bench/implbench/harness/dispatch.py`; edit `bench/implbench/harness/fixtures.py`,
`bench/implbench/harness/ref_protection.py`, `bench/implbench/harness/gates.py`, and
`bench/implbench/harness/validate.py`; edit `src/agent_redis_bridge/bridge.py`; new
`bench/implbench/tests/test_controller.py`, `bench/implbench/tests/test_close_recovery.py`,
`bench/implbench/tests/test_close_branches.py`, and
`tests/test_bridge_bakeoff_completion.py`; extend `bench/implbench/tests/test_cli.py` to remove legacy
dispatch auto-commit/host-completion/worktree-remove expectations; run ordinary-path regressions
`tests/test_bridge.py`, `tests/test_bridge_handle_raw.py`, `tests/test_bridge_worktree.py`, and
`tests/test_completion_gate.py`, plus `bench/implbench/tests/test_cell_runtime.py`,
and explicitly migrate `bench/implbench/tests/test_fixtures.py`,
`bench/implbench/tests/test_ref_protection.py`, `bench/implbench/tests/test_gates.py`, and
`bench/implbench/tests/test_validate.py`.

- [ ] RED phase graph tests: any terminal event enters one CLOSING dispatcher; applicable ordered
  close phases; UNKNOWN, NOT_DELIVERED, ordinary import, BUDGET_FAILED, score, evidence, destroy;
  no backward transition or duplicate model/import/score.
- [ ] RED every-exit tests for the normative ten-step close sequence, early provisioning failure,
  never-started/unreachable Git status, empty clean/dirty, hostile seal, non-empty, budget empty/
  non-empty, budget+infrastructure, importer/scorer failure, census failure, and root deletion.
- [ ] RED crash after each journal commit and before each side effect; restart resumes only first
  uncommitted action, re-running only idempotent cleanup/absence probes.
- [ ] RED B08 ordering/durability: no G1/G4 launch before the pre-scorer input attestation is
  append+fsynced, re-read, and verified; in-memory-only, post-launch write, re-read mismatch, or full
  record release fails closed; scorer receives only its digest-bound projection. The pre-scorer
  schema rejects G4 receipts. Only after G4 receipts return and the full G4 topology is reaped may
  the post-G4 receipt attestation bind the pre-scorer digest plus exact ordered receipt-list digest;
  missing/early/reordered/mismatched post-G4 evidence forbids G4 classification and report release.
- [ ] RED repair authorization state transitions: attempt zero is scheduled; only an authoritative
  G0 `UNKNOWN` in a closed infrastructure category may append+fsync the next consecutive attempt's
  authorization before redispatch. Missing, late, duplicate, skipped-number, model-failure, and
  gate-FAIL authorizations fail closed and can never replace the original attempt.
- [ ] Implement controller using Tasks 3–8 APIs; delete/disable legacy scored-path completion,
  auto-commit, host-Git, and worktree-remove behavior. Migrate dispatch to the bakeoff cell/attempt
  ref and classifier APIs, then delete T7's ordinary run/result adapter and T8's ordinary
  `evaluate_gate` adapter; ordinary CLI behavior remains covered independently.
- [ ] RED/GREEN bridge scored-mode selection: `Bridge.process_request` uses receipt-only completion,
  forces `AGENT_AUTO_COMMIT=0`, and never invokes `completion_gate`, orchestrator commit, or host
  `git -C <cell>`; ordinary non-bakeoff requests preserve existing behavior. Each forbidden call has
  a monkeypatch tripwire that fails if it re-enters the scored path.
- [ ] RED before adapter deletion: an automated importer/call-site census over the complete
  `bench/implbench/harness/**/*.py` and `bench/implbench/tests/**/*.py` trees fails until every
  production or test importer of T7's run/result adapters and T8's `evaluate_gate` adapter is
  migrated; the bakeoff controller/dispatch tripwires separately fail if either adapter is used.
  Delete the symbols only after the census reaches zero.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_controller.py bench/implbench/tests/test_close_recovery.py bench/implbench/tests/test_close_branches.py bench/implbench/tests/test_cell_runtime.py bench/implbench/tests/test_fixtures.py bench/implbench/tests/test_ref_protection.py bench/implbench/tests/test_gates.py bench/implbench/tests/test_validate.py bench/implbench/tests/test_cli.py tests/test_bridge_bakeoff_completion.py tests/test_bridge.py tests/test_bridge_handle_raw.py tests/test_bridge_worktree.py tests/test_completion_gate.py`.

**Done:** branch coverage proves evidence durable before descriptor-safe cell destruction.

**Commit:** `feat(implbench): add single journal-driven production close`.

## Task 10 — strict seat preflight and fourteen aggregate readiness gates

**Files:** extend `scripts/seat-preflight`; new `bench/implbench/harness/readiness.py`; new
`bench/implbench/tests/test_seat_preflight_bakeoff.py`,
`bench/implbench/tests/test_readiness_gates.py`, `bench/implbench/tests/test_readiness_hostile.py`;
extend `tests/test_seat_preflight.py` regression expectations; new
`bench/implbench/tests/test_readiness_live.py` marked `live_bakeoff` to bind the T4/T5 boundary
modules into exact Gate 9–14 production paths and assert no skipped or substituted fake counts.

- [ ] RED Gate 1–14 tests mapped one-to-one to frozen spec section 7, with
  `{gate_id,status,evidence_digest,started_at,ended_at}` and aggregate PASS only for fourteen PASS.
- [ ] Implement separate real representative cells per arm/fixture; registry ping, exact-text
  runtime acknowledgements, `agent-dispatch --worktree ... --worktree-cleanup keep` smoke, model/
  version/config verification, retire/base-clean proof, home/key/credential/network denial.
- [ ] Implement and RED-test the capability contract: persist every arm's effective tool surface,
  map each tool to frozen capability classes, run positive probes for every required class and
  negative probes for every prohibited class, and refuse the affected pair before calibration when
  classes are unknown, unequal, or cannot be constrained without widening either arm.
- [ ] Add live tool/ingress/service budget matrix, all close branches, export/cell poisons,
  importer/scorer/bytecode attacks, ACL lifecycle, service<=importer boundaries, and prune canary.
  The hermetic canary calls T7's protected-ref/prune core directly; it does not depend on T11's CLI.
- [ ] Define Gate 14's injected `known_good_calibration(manifest, cell_factory)` callback protocol in
  `readiness.py`. Its hermetic controller test supplies a strict fake and proves `validate` then the
  callback run before PASS. T12 owns and tests the production callback implementation/binding; T14
  supplies the live seat and will not accept a fake.
- [ ] Ensure aggregate controller independently repeats, never trusts seat JSON, and uses the same
  production close—no second teardown algorithm.
- [ ] GREEN hermetic: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_seat_preflight_bakeoff.py bench/implbench/tests/test_readiness_gates.py bench/implbench/tests/test_readiness_hostile.py tests/test_seat_preflight.py`.

**Done:** removing any hostile fixture or cleanup proof prevents aggregate PASS.

**Commit:** `feat(implbench): implement fourteen strict readiness gates`.

## Task 11 — CLI, immutable evidence package, report schema, safe prune

**Files:** refactor `bench/implbench/harness/cli.py`, `bench/implbench/harness/evidence.py`,
`bench/implbench/harness/report.py`, and
`bench/implbench/harness/provenance.py`; edit `bench/implbench/harness/dispatch.py`,
`bench/implbench/harness/fixtures.py`, and `bench/implbench/harness/ref_protection.py`; update
`scripts/implbench`; new
`bench/implbench/tests/test_cli_v2.py`, `bench/implbench/tests/test_evidence_package.py`,
`bench/implbench/tests/test_report_v2.py`, `bench/implbench/tests/test_prune_v2.py`; migrate or
replace legacy expectations in `bench/implbench/tests/test_cli.py`,
`bench/implbench/tests/test_report.py`, `bench/implbench/tests/test_provenance.py`,
`bench/implbench/tests/test_records_v2.py`, `bench/implbench/tests/test_authlog.py`,
`bench/implbench/tests/test_fixtures.py`, and `bench/implbench/tests/test_ref_protection.py`.

- [ ] RED CLI tests for `validate`, `preflight --manifest`, `calibrate --manifest --seat`, `pilot`,
  `run`, `report --evidence`, `prune --before --evidence-root`; hard-refuse concurrency != 1. T11
  owns parser, manifest/package guards, and injected handler delegation with strict fakes; T12 owns
  the production calibrate/pilot/run handler bodies and re-runs these CLI tests after binding.
- [ ] RED exact evidence-root tests, public/private split, no secret/dynamic diagnostics, immutable
  rows, protected live/final refs, absent `git-refs.txt` until final close, accounting, and prune
  CLI delegation to T7's protected-ref core, mandatory-root refusal, all four production canaries,
  successful eligible ordinary deletion, and descriptor safety.
- [ ] RED closed-package finality: once a valid sealed `git-refs.txt` exists, `preflight`,
  `calibrate`, `pilot`, `run`, execution-resume, and every package-mutating path fail before mutation;
  only read-only `validate` and `report` succeed, while `prune` may only externally scan protection
  and must never mutate the package.
- [ ] After migrating CLI/report/provenance consumers to authenticated records and mandatory-root
  prune, delete T3's ordinary `Recorder` adapter and T7's ordinary no-root prune adapter. GREEN
  proves no compatibility symbol remains reachable.
- [ ] RED before deleting either adapter: automated importer/call-site censuses over the complete
  `bench/implbench/harness/**/*.py` and `bench/implbench/tests/**/*.py` trees fail until every
  `Recorder` importer uses authenticated records and every no-root prune importer uses the mandatory
  evidence-root API. CLI, dispatch, records/authlog, and direct ref-protection/fixture tests remain
  red until both censuses reach zero; only then delete symbols.
- [ ] RED `pair-analysis-v1`: two separate pair grids, exact sign tests + underpowered, named hard-floor
  regressions, delivery/TDD shapes, successful median/p95 + failure counts, variance/asymmetry, four
  closed evidence shapes, no rank/composite/automatic promotion.
- [ ] RED frozen analysis details: exact `(model family, task, repetition)` matching; PASS/FAIL
  win/tie rule; non-scoreable missing/UNKNOWN/NOT_SCORED/pin-mismatch exclusion without imputation;
  attempt-zero selection and the lowest scoreable attempt in a contiguous pre-dispatch authenticated
  repair chain, rejecting gaps, late authorization, non-consecutive numbers, model failure, and gate
  FAIL; four-repetition per-task counts and repeatability threshold (at least three wins, zero
  reverse); two-sided exact sign p=`min(1,2*BinomCDF(min(w,l);n,0.5))`; central equal-tail 95%
  Clopper-Pearson beta-quantile interval including `n=0` p=`1`/interval=`[0,1]`; `<8` non-ties
  underpowered; and a winner only at `p <= 0.05` with more wins and zero G3/G5/G6/G7 FAIL across
  every selected authoritative attempt for that arm, including G1 ties. Golden fixtures cover
  shared failures, boundary win counts, repair alternatives, and otherwise-tie/mixed outcomes.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_cli_v2.py bench/implbench/tests/test_evidence_package.py bench/implbench/tests/test_report_v2.py bench/implbench/tests/test_prune_v2.py bench/implbench/tests/test_records_v2.py bench/implbench/tests/test_authlog.py bench/implbench/tests/test_fixtures.py bench/implbench/tests/test_ref_protection.py bench/implbench/tests/test_cli.py bench/implbench/tests/test_report.py bench/implbench/tests/test_provenance.py`.

**Done:** report/evidence validators reject every forbidden or missing field mechanically.

**Commit:** `feat(implbench): add sealed CLI evidence report and prune contracts`.

## Task 12 — calibration, pilot seal, stop rules, full matrix machinery

**Files:** new `bench/implbench/harness/phases.py`, `bench/implbench/harness/runner.py`; extend
`bench/implbench/harness/controller.py`, `bench/implbench/harness/cli.py`; new
`bench/implbench/tests/test_phases.py`, `bench/implbench/tests/test_pilot_seal.py`,
`bench/implbench/tests/test_stop_rules.py`, `bench/implbench/tests/test_matrix_runner.py`.

- [ ] RED calibration tests: hermetic suite, adversarial validation, known-good Codex clearing at
  least one pinned task in every C1–C7 cluster, then one unscored task through all four exact scored
  paths; any uncleared cluster refuses acceptance; no scored refs/results.
- [ ] Implement and bind the production `known_good_calibration` callback consumed by T10 Gate 14;
  RED proves the default runtime cannot silently substitute T10's hermetic fake or skip the callback.
- [ ] RED pilot tests: repetition 1 exact order, append-only invalid attempts, rerun only classified
  infrastructure UNKNOWN with new attempt IDs, model outcomes retained, no comparison, complete seal
  digest over manifest/config/refs/journal, no `git-refs.txt`.
- [ ] RED full-run tests: require unchanged valid pilot seal; continue repetitions 2–4; one at a
  time; pre-dispatch ten stop rules; pair-scoped halt; no quiet retry; final 128 indices exactly once;
  final refs/evidence freeze only after close.
- [ ] Implement phases using Task 9 controller and Task 11 evidence APIs.
- [ ] GREEN: `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_phases.py bench/implbench/tests/test_pilot_seal.py bench/implbench/tests/test_stop_rules.py bench/implbench/tests/test_matrix_runner.py`.
- [ ] Re-run first-breaking controller/CLI regressions after production phase binding:
  `PYTHONPATH=bench .venv/bin/python -m pytest -q bench/implbench/tests/test_controller.py bench/implbench/tests/test_cli_v2.py bench/implbench/tests/test_cli.py`.

**Done:** a changed pilot byte, stopped pair, duplicate/missing index, or model-result rewrite fails.

**Commit:** `feat(implbench): add calibration pilot and matrix phase machine`.

## Task 13 — full adversarial integration, documentation, and inertness

**Files:** new/extended `bench/implbench/tests/test_bakeoff_integration.py`,
`bench/implbench/tests/test_bakeoff_deny_proofs.py`; update
`docs/pipeline-operating-manual.md`, `CHANGELOG.md`,
`docs/superpowers/notes/2026-07-13-openinterpreter-pi-bakeoff-handoff.md`. No other
file may be edited; if integration requires a packaging/CI path not already owned by T1, append an
Escalation and stop T13 rather than expanding scope in the worker report.

- [ ] RED: add hermetic end-to-end known-good and every stop/failure path, with deterministic evidence
  package golden validation and restart at every close phase.
- [ ] RED: add mechanical tripwires for legacy host completion/auto-commit/Git/worktree-remove paths,
  secret argv/env/log leaks, extension loading, active-checkout writes, and scored-cell execution in
  unit tests; show the targeted tests fail when each guard is monkeypatched away.
- [ ] Run targeted tests after each task, then full main and bench suites, doc recipes/drift,
  `git diff --check`, and fresh `uv sync --extra bench` help/validate smoke.
- [ ] Document exact operator commands and failure interpretation without embedding credentials.
- [ ] GREEN: `env -u IMPLBENCH_BATTERY_KEY .venv/bin/python -m pytest -q && PYTHONPATH=bench env -u IMPLBENCH_BATTERY_KEY .venv/bin/python -m pytest -q bench/implbench/tests && scripts/check-doc-recipes && scripts/check-doc-drift && git diff --check`.

**Done:** clean full suite; all skip reasons audited; no scored cells; docs match actual CLI.

**Commit:** `test(implbench): close bakeoff adversarial integration` and
`docs(implbench): add isolated bakeoff runbook`.

## Task 14 — orchestrator-only live readiness + unscored calibration evidence

This is a deliberate no-code/no-commit gate; it never runs a pilot or scored cell.

This is not a Luna implementation task and has no repository edit or commit. It runs only after
Tasks 1–13 are integrated, implementation-reviewed to audited zero P0/P1, and provider credentials
are rotated into controller-owned mode-0600 external sources. Evidence is written only beneath the
manifest-pinned external controller evidence root: `preflight/`, append-only gate/result NDJSON,
redacted seat configs, and the implementation handoff's referenced value-free digests. Any code or
tracked-file change aborts and returns to implementation review.

- [ ] RED precondition: before live invocation, a synthetic manifest with one bad pin and a synthetic
  skipped/UNKNOWN gate must make both `scripts/implbench validate` and preflight exit nonzero, create
  no scored refs, and leave no UID/ACL/root. Record exact non-secret output digest.
- [ ] From a fresh clean controller clone, verify all frozen binary/config/profile/source pins and
  `git status --porcelain` empty before and after every command.
- [ ] GREEN live command: run `scripts/implbench validate` and
  `scripts/implbench preflight --manifest <absolute-controller-owned-manifest>`
  through all exact live boundaries. No skip, UNKNOWN, or substituted fake may count as PASS.
- [ ] Run `.venv/bin/python -m pytest -q -m live_bakeoff tests bench/implbench/tests` as part of the live command and prove the
  registered live sentinel plus every required Seatbelt/UID/ACL/Valkey boundary test executed rather
  than being deselected.
- [ ] As the final subproof inside aggregate Gate 14, run `validate` and known-good Codex calibration
  clearing every C1–C7 cluster;
  Gate 14 and aggregate readiness cannot PASS before both succeed. After fourteen gates PASS, run
  the separate four-seat unscored calibration. It creates no pilot/final analysis.
- [ ] Persist value-free gate records, private quarantined diagnostics, cleanup/absence proofs, exact
  commands, environment digests, and a handoff. Re-run validation from a second clean process.
- [ ] If any gate is FAIL/UNKNOWN, stop; retain evidence append-only; do not pilot.
- [ ] GREEN verification: a second clean process runs read-only `validate` over the external package,
  confirms fourteen PASS digests, calibration attestations, zero live UID/ACL/root/client resources,
  clean Git status, and absence of pilot/final/scored refs.

**Done:** fourteen independently evidenced PASS gates, clean checkout, zero live cell resources,
valid calibration package, and implementation panel approval. Only then may the orchestrator begin
the separately controlled pilot and full scored run.

**Commit:** none by design; Task 14 is an external live evidence gate. The orchestrator records
commands, exit codes, evidence digests, and resource-absence proofs in the controller-owned package
and updates the tracked handoff only in a later separately reviewed documentation commit.

## Integration acceptance matrix

| Requirement | Authoritative proof |
|---|---|
| OI exact adapter/tool interception/retirement | T1 protocol + containment tests |
| exact arms/corpus/controls/schedule | T2 manifest/schedule goldens |
| append-only schemas and auditability | T3 mutation/replay/fsync tests |
| OS isolation and secret denial | T4/T5 hermetic construction + T14 live lifecycle proofs |
| Git-only controlled mutation and completion | T6 hostile RPC/seal tests |
| no prior-result contamination / safe import | T7 census/import attacks |
| scoring confidentiality and correct blame | T8 six-role/classifier matrix |
| every exit closes once and recovers | T9 crash-point suite |
| all fourteen readiness blockers | T10 gate map + T14 live records |
| immutable evidence/no leaderboard | T11 validators |
| pilot/full stop and append-only semantics | T12 phase tests |
| repo-wide regression/inertness | T13 full suites and tripwires |

## Escalations

Empty at plan authoring. A worker appends: task, frozen requirement, conflicting code/evidence,
failed command, and why no compliant implementation is possible. The affected task stops; downstream
tasks do not branch from it.

# Open Interpreter vs Pi bakeoff — implementation specification

**Status:** frozen after audited r27 unanimous zero-P0/P1 approval. Reviewed revision
`614579031700af95111493b563f7d7bb39065aff`, specification blob
`9fbf8bacf045395ec00ca55ba8f5a1e3479f6ad9`.
**Depends on:** frozen design
`docs/superpowers/specs/2026-07-13-openinterpreter-pi-isolated-bakeoff-design.md` at exact commit
`166eb76bcf50ea28d44bfccf3604b8c824bf698a`, blob
`c5981bcdc3bf4d52a5cdfaff58a26df336771fb7`; this specification may make that design executable but
may not weaken it. The manifest records both identifiers and readiness rejects any mismatch.
**Execution base:** unset until every readiness acceptance gate below passes from a clean checkout.
**Scored cells run:** none.

## 1. Delivery boundary

Implementation is complete only when the repository can create four isolated bakeoff arms, prove
all fourteen frozen readiness blockers, run a known-good calibration, and emit an immutable execution
manifest without exposing secrets or executing submitted code on the controller. Merely adding an
Open Interpreter engine or making existing unit tests pass is insufficient.

The work is divided into seven components with explicit ownership:

1. ARB engine and dispatcher integration;
2. controller, manifest, schedule, and append-only evidence;
3. cell runtime supervisor and three seat-side planes;
4. Git shim/service, receipt log, completion verifier, and importer;
5. hidden/public test sandbox and G0-G7 classification;
6. strict seat plus aggregate readiness preflight;
7. reporting, calibration, pilot, and matrix runner.

### 1.1 Exact arm/runtime pins

The manifest and Gate 4 exact-match these four and no others:

| Pair | Arm | Engine | Provider/model | Harness | Agent-ID prefix | Required fresh-process state |
|---|---|---|---|---|---|---|
| GLM | `glm-pi` | `pi-sdk` | `zai/glm-5.2` | Pi | `pi-sdk-agentredisbridge-bake-glm52` | `BRIDGE_PI_RETIRE_AFTER_TURN=1`; fresh per-cell `PI_CODING_AGENT_DIR` |
| GLM | `glm-zcode` | `openinterpreter` | `zai-coding-plan` / `glm-5.2` | `zcode` | `interpreter-agentredisbridge-bake-glm52-zcode` | `BRIDGE_INTERPRETER_RETIRE_AFTER_TURN=1`; fresh per-cell `INTERPRETER_HOME` |
| Kimi | `kimi-pi` | `pi-sdk` | `kimi-coding/k2p7` | Pi | `pi-sdk-agentredisbridge-bake-k2p7` | `BRIDGE_PI_RETIRE_AFTER_TURN=1`; fresh per-cell `PI_CODING_AGENT_DIR` |
| Kimi | `kimi-cli` | `openinterpreter` | `kimi-for-coding` / `k2p7` | `kimi-cli` | `interpreter-agentredisbridge-bake-k2p7-kimicli` | `BRIDGE_INTERPRETER_RETIRE_AFTER_TURN=1`; fresh per-cell `INTERPRETER_HOME` |

Every scoreable arm has requested and independently runtime-acknowledged effective reasoning exactly
`medium`; any other, absent, echoed, or unequal value makes that pair non-scoreable.

### 1.2 Exact experimental unit and corpus

One cell is `pair × arm × task fixture SHA × repetition`. The only scoreable corpus is the committed
Implementor Bench v1 root `bench/implbench/fixtures/` with exactly these eight task IDs and no
extras: `c1-permissive-boundary`, `c1-token-bucket`, `c2-parser`, `c3-refactor`, `c4-rail`,
`c5-artifact`, `c6-scope`, and `c7-provenance`. Manifest creation pins every fixture SHA; validation
rejects a missing/extra task, root mismatch, symlink, or SHA drift. The closed product is
`2 pairs × 2 arms × 8 tasks × 4 repetitions = 128` scored dispatches.

## 2. Repository changes

### 2.1 Open Interpreter engine

Add `src/agent_redis_bridge/engines/openinterpreter.py` implementing the existing engine base
contract and register `openinterpreter -> interpreter` everywhere engine/tool mappings and CLI
choices are defined, including `src/agent_redis_bridge/bridge.py`,
`src/agent_redis_bridge/engines/__init__.py`, `scripts/agent-dispatch`, and control/status CLIs.

The adapter must:

- launch Open Interpreter exactly `0.0.21` as `interpreter app-server --listen stdio://` without a
  shell, and separately pin/verify interpreter binary, harness, adapter, and provider-client digests;
- require explicit provider, model, and harness (`zcode` or `kimi-cli`), never infer defaults;
- speak the app-server protocol with bounded frames and terminal-state validation;
- surface failed `turn/completed` error payloads as engine failures;
- route every shell/file/code tool request into the cell tool-plane broker; no in-process execution;
- expose requested/effective generation controls and their independent acknowledgement source;
- support fresh context only for scored turns and retire after every turn through a dedicated
  `BRIDGE_INTERPRETER_RETIRE_AFTER_TURN=1` contract;
- return engine/provider/model/harness/version/control provenance as structured data, not prose.

Tests: protocol success/failure, malformed/oversized frame, missing harness/provider, tool
interception, no direct execution, retirement, provenance, control acknowledgement, and process
cleanup. Existing engine-containment tests must include this adapter.

### 2.2 Declared bench runtime

Add the bench dependencies, including PyYAML, to an explicit optional dependency group in
`pyproject.toml`. `scripts/implbench` must select the repository environment deterministically and
must pass `--help` after a fresh `uv sync --extra <bench-extra>`. It may not fall back to system
Python.

### 2.3 Implementor Bench packages

Create focused modules rather than extending `dispatch.py` into a monolith:

```text
bench/implbench/harness/
  manifest.py          immutable environment + matrix manifest
  schedule.py          frozen 128-cell order and pilot projection
  controller.py        state machine and append-only attempt orchestration
  cell_runtime.py      canonical roots, UID/ACL/process lifecycle
  sandbox.py           frozen Seatbelt profiles and launch verification
  git_service.py       closed status/add/commit service
  receipts.py          authenticated fsynced Git/public-test logs
  importer.py          descriptor-held quarantine snapshot and validation
  completion.py        sealed completion and post-import attestations
  scorer_sandbox.py    G1 and keyless G4 launch topologies
  readiness.py         fourteen sub-gates and aggregate result
```

The existing `cli.py`, `dispatch.py`, `gates.py`, `provenance.py`, `scoring.py`, `evidence.py`, and
`report.py` become thin callers of those contracts. Cycles between controller, scorer, and importer
are forbidden.

## 3. Authoritative schemas

Use versioned frozen dataclasses/Pydantic-equivalent validation with canonical JSON serialization.
Unknown fields, duplicate keys, non-canonical numbers, and unknown schema versions fail closed.

### 3.1 Environment manifest

The evidence package's single `manifest.json` (schema version `manifest-v2`) is written atomically
before calibration and contains:

- run ID, design/spec/plan commits, source/base/corpus/fixture SHAs, and the canonical
  `realpath(ARB_SRC_REPO)`;
- four arm definitions and exhaustive requested/effective/verified-via control maps;
- model/provider/harness/adapter identifiers and content/version digests;
- macOS build, Python/Node/Git versions, generated Seatbelt/Mach blobs and digests;
- importer, scorer, encrypted-battery artefact, opaque key-version, and public-suite pins;
- the bakeoff ref namespace policy, planned cell identities, and controller evidence-root
  configuration used to locate final sealed ref indices (not a mutable post-run ref set);
- exact Git-RPC constants: `max_frame_bytes=1048576`, `max_path_bytes=4096`,
  `max_components_per_path=256`, `max_component_bytes=255`, `max_paths_per_request=1024`,
  `max_in_flight=8`, `status_rate_per_second=4`, and `status_burst=8`;
- `final_tree_digest_version`, capability map, time/resource budgets, random seed, full schedule,
  stop rules, rerun rules, and analysis rules.
- explicit empty allowlists for role profiles, project instruction files, optional skill packs,
  memory MCPs, and unrelated extensions. Manifest validation and every cell launch prove none is
  configured, mounted, discoverable, or loaded; a non-empty or unverifiable surface is non-scoreable.

Startup and every readiness phase recompute the source realpath and reject a mismatch, symlink, dirty
source, or SHA drift. After atomic creation, any byte change or pin drift requires a new run ID. Secrets, credential
paths visible to seats, plaintext battery data, and secret-derived reusable digests are forbidden.

### 3.2 Identity and records

Every record carries `run_id`, `cell_id`, `attempt_id`, attempt number, pair, arm, task, repetition,
schedule index, fixture SHA, `model_declared`, `model_verified_via`, engine version, harness version, corpus version,
config digest, capability manifest digest, `reasoning_requested`, `reasoning_effective`,
`reasoning_verified_via`, start/end timestamps, wall time, terminal status, retry count, tool-call
count, schema version, and prior-record digest. Provider/engine errors use a closed error enum and a
bounded quarantined diagnostic digest; concrete errors are never discarded or exposed publicly.
The same requested/effective/verified-via triple is present for every exposed generation/runtime
control: temperature, top-p/top-k, seed, penalties, maximum output, stop behavior, tool choice,
parallel-tool behavior, retry/backoff, all timeouts, and any additional exposed control. Attempt evidence is append-only NDJSON with
fsync after each record; replacement or truncation is detected at startup.
Readiness census records additionally carry `census_evidence_digest` and their gate/phase identity.
The canonical controller-private record (schema, phase, violation enum, expected/observed
digests/counts) is durably stored under `preflight/census-private.ndjson`; it contains no dynamic ref
names/content and is retained so validation can recompute the public digest after cell destruction.

### 3.3 Git receipt

Internal fixed-width fields:

```text
cell_id, attempt_id, fixture_root_oid, ordered_parent_oids[1], commit_oid, tree_oid,
changed_paths, tree_digest, tree_digest_version, head_oid, dirty, controller_sequence, nonce
```

The controller assigns sequence and authenticates/fsyncs each receipt before acknowledging commit.
Service-reported paths are never authoritative. Every receipt `tree_digest_version` must equal the
manifest-pinned `final_tree_digest_version`; the completion verifier and importer reject the entire
attempt before import on a missing, unknown, or mismatched receipt version and emit the authoritative
infrastructure G0-G7 `UNKNOWN` cascade; version-pin failure is never model non-delivery.

### 3.3.1 Model budget failure record

The controller ingress independently enforces frame/path/request/rate limits; its supervisor
independently observes only OS-accounted CPU, memory, wall-time, process, and disk limits. The
controller directly authors sealed records for those observations, including tool-command and
ingress limits.
Operation-internal status/hash/stage/tree/commit byte/object/count limits require a graceful Git-service
candidate. A hard kill without
independent OS accounting or a valid candidate is infrastructure `UNKNOWN`, never invented model
FAIL. On a graceful model-attributable budget failure, the Git service emits a bounded candidate
`{cell_id, attempt_id, operation, reason, budget_dimension, limit, observed}` over the authenticated
per-attempt channel. The untrusted service never chooses controller sequence or nonce and may emit only
`status|hash|stage|tree|commit`; a candidate containing `tool|ingress` is rejected. The closed record
`operation` enum is `tool|ingress|status|hash|stage|tree|commit`; `reason` is the closed enum
`MODEL_BUDGET_EXCEEDED`; and
`budget_dimension` is one of the manifest-pinned limit names. The controller validates fixed-width
identities/enums and bounded integers, adds `controller_sequence` and a controller nonce, authenticates,
appends and fsyncs the sealed record before acknowledging a model-initiated failed RPC. For a
controller-initiated final-status call after model clients are dead it performs the same validation
and fsync without attempting a model acknowledgment. A missing/malformed/unfsynced failure record is infrastructure
`UNKNOWN`; only this controller-sealed record authorizes model-budget G0 `FAIL` precedence over an
incomplete seal. Controller-authored ingress/tool records and controller-sealed Git-service
candidates use the identical append/fsync schema.

The private census record canonically contains schema version, `run_id`, `cell_id`, `attempt_id`,
gate identity, phase (`export|cell`), expected ref
and object-set digests/counts, observed ref and object-set digests/counts, and a closed violation
enum: `EXTRA_REF|MISSING_REF|EXTRA_OBJECT|MISSING_OBJECT|OBJECT_SET_MISMATCH|INVALID_OBJECT_TYPE`.
Its public `census_evidence_digest` is SHA-256 over that canonical private record; verifiers recompute the
canonical bytes/digest and require the recorded `export|cell` phase and violation enum to match the
tripwire. Dynamic ref names
and diagnostics remain quarantined.

### 3.4 G4 receipt

The internal authenticated G4 record is:

```text
cell_id, attempt_id, commit_oid, public_suite_oid, public_suite_digest,
public_suite_digest_version, outcome_enum, controller_sequence, nonce
```

OID/digest fields are fixed-width validated bytes, the suite identity must equal the frozen task pin,
outcome is a closed enum, and controller sequence/nonce prevent replay. Public evidence exposes only
those non-secret fixed-width fields plus enums/bounded integers.

### 3.5 Completion and scoring attestations

The sealed completion record uses the mandatory append-only identity envelope from section 3.2. Its
fixed internal payload has exactly these fields: `cell_id`, `attempt_id`, `fixture_root`, `receipts`,
`head`, `dirty`, `final_tree_digest`, and `final_tree_digest_version`. `receipts` is the
ordered Git-receipt list; `dirty` covers the whole worktree. The verifier has explicit
empty/non-empty receipt branches. The Git-service `status` payload is exactly `{head, dirty,
final_tree_digest, final_tree_digest_version}`. After control/tool processes are dead, the controller
seals a pure projection of the fsynced attempt log plus that final status; it does not query host
Git. The digest is a descriptor-relative, no-follow canonical materialization of the whole fixed
worktree excluding only Git metadata. The walk rejects escaping or symlinked directory components,
missing or unknown digest versions, and any mismatch with the manifest-pinned version before seal.
It includes sorted
directory structure, normalized path
bytes, file type/mode, executable bit, sizes, regular-file bytes, and symlink-target bytes, including
all uncommitted and untracked state; it excludes timestamps, uid/gid, and inode numbers. `dirty`
means that materialization differs from the reported HEAD tree under the pinned digest version.

Empty requires the controller-pinned fixture root, no receipts, the pinned digest version, and a
bound final filesystem digest. It records `not-delivered` whether `dirty` is true or false and never
compares the final tree with the fixture or requires a clean seal. Non-empty additionally requires
every receipt authenticated and chain-consistent, `head == receipts[-1].commit_oid`, `dirty ==
false`, and `final_tree_digest` equal to the canonical materialization digest of the last receipt's
tree under the identical pinned version. A failed model-owned predicate is non-delivery and forbids
import; infrastructure G0 `UNKNOWN` retains precedence. Two separate controller-owned
record/durability boundaries replace one temporally impossible combined attestation; neither adds a
UID. After import and before any G1/G4 launch, the closed authenticated **pre-scorer input
attestation** binds environment-manifest, completion, and imported-graph digests. It is
append+fsynced, re-read, and verified; only its verified digest-bound projection reaches scorers. An
in-memory object, post-launch write, or full-record scorer release is insufficient. After G4 returns
sequenced receipts and its complete topology is reaped, the closed authenticated **post-G4 receipt
attestation** binds the verified pre-scorer-attestation digest and ordered G4-receipt-list digest. It
is append+fsynced, re-read, and verified before G4 classification or report release. The pre-scorer
record forbids a G4-receipt-list field. Replay, identity/pin/version mismatch, reordered receipts,
unknown version, or digest mismatch at either boundary takes the authoritative infrastructure G0-G7
`UNKNOWN` cascade and stops the run.

Public evidence permits enums, booleans, bounded integers, and required non-secret fixed-width
OID/digest/version fields only. Dynamic diagnostic strings remain quarantined.

## 4. Cell runtime and sandbox contract

The canonical root is `/Users/Shared/arb-implbench/<run-id>` with controller ownership and mode
`0700`. Narrow macOS POSIX ACLs grant only the required traverse/read/write rights to each ephemeral
plane UID on its exact paths; no broad group/world permission or `0777` fallback is allowed.
`ARB_SRC_REPO` must be a clean, non-symlinked absolute source path pinned to the execution base.

The controller clone is created with `--no-local`, owns its object store, and is never exposed to a
seat. For each cell, the controller first creates a temporary quarantine export repository whose
only refs are controller-internal `refs/arb-export/base` and `refs/arb-export/fixture`, pointing to
the pinned base and that cell's fixture OIDs, and whose only object closure is those two tips. Tags
and every `refs/implbench/*` ref are excluded. The cell is then a `--no-local --no-tags`
own-object-store clone of that export with an exact allowlisted export refspec. Before cloning, a
controller descriptor-safe export census requires exactly those two refs and the exact union of
their transitive object closures. After cloning, an independent complete cell ref/object census
proves no
prior result ref or object entered it. A default clone of the controller/source repository is
forbidden. Both clones explicitly use Git's `files`
ref backend, and readiness rejects `reftable`. The linked worktree exists only inside that disposable
cell clone. Every cell also receives a fresh state home, bus namespace, and three distinct ephemeral
UIDs: control, tool, Git service.

The controller mints the seat agent ID as the exact arm prefix from section 1.1 plus `-<cell_suffix>`,
where `cell_suffix` is a fixed lowercase base32 encoding of the immutable `cell_id` digest and is
unique in the manifest. Registry identity, lease, engine process, context/thread/session ID, state
home, and bus namespace must all be absent before launch and retired after close. Resume, fork,
continuation, reconnect-to-thread, warm process, or any identity/context reuse path is forbidden;
Gate 5 and each scored dispatch exact-check fresh values and stop the pair on reuse.

`cell_runtime.py` implements one state machine:

```text
ALLOCATED -> PREFLIGHTED -> DISPATCHED
Any terminal event -> CLOSING -> [each applicable ordered close phase] -> CLASSIFYING
    CLASSIFYING
       |-> UNKNOWN_EVIDENCED -> DESTROYED
       |-> NOT_DELIVERED -> EVIDENCED -> DESTROYED
       |-> non-budget verified non-empty seal -> IMPORTED
       |      |-> import/fsck/materialization/attestation failure -> UNKNOWN_EVIDENCED -> DESTROYED
       |      `-> SCORED
       |             |-> scorer launch/supervisor failure -> UNKNOWN_EVIDENCED -> DESTROYED
       |             `-> EVIDENCED -> DESTROYED
       `-> BUDGET_FAILED
           |-> verified non-empty seal -> IMPORTED
           |      |-> import/fsck/materialization/attestation failure -> UNKNOWN_EVIDENCED -> DESTROYED
           |      `-> SCORED
           |             |-> scorer launch/supervisor failure -> UNKNOWN_EVIDENCED -> DESTROYED
           |             `-> EVIDENCED -> DESTROYED
           `-> empty/incomplete/non-deliverable seal -> EVIDENCED -> DESTROYED
```

Only one classification branch may be taken. The closed decision table is: any already observable
independent authentication, version-pin, seal/status/verifier, provisioning, or controller fault
takes `UNKNOWN_EVIDENCED`; otherwise any authenticated, fsynced `MODEL_BUDGET_EXCEEDED` record always
takes `BUDGET_FAILED` with provisional G0 `FAIL`, then sub-branches on non-empty versus
empty/incomplete/non-deliverable seal; otherwise use ordinary non-delivery or import. A later
import/fsck/materialization/attestation or scorer-launch/supervisor infrastructure failure from
either import branch replaces the provisional classification with authoritative G0-G7 `UNKNOWN`.
`[each applicable ordered close phase]` means the journal executes the numbered production-close
actions whose prerequisites exist and records bounded not-applicable results for the rest; an early
ALLOCATED/PREFLIGHTED failure therefore never requires QUIESCING, sealing, or snapshotting.
Budget precedence covers an incomplete seal only when the authenticated owning budgeted operation
directly prevented completion and all available receipt/version/authentication evidence is valid.
A verified non-empty seal still imports and scores the
trusted materialization; only an empty/incomplete/non-deliverable seal takes its non-scored terminal
branch. Otherwise, when G0 is not `UNKNOWN`, empty receipts or an
authenticated model-owned dirty/chain-inconsistent seal use `NOT_DELIVERED` and never
importer/scorer; unauthenticated/incomplete material or any other infrastructure
seal/status/verifier failure sets G0 and all dependent gates to `UNKNOWN` regardless
of the prior dispatch state, durably appends authoritative G0-G7 UNKNOWN records through
`UNKNOWN_EVIDENCED`, and then destroys the cell. A model-attributable status resource-budget breach remains
G0 `FAIL`, not an infrastructure status failure. Only a verified
non-empty seal with fully authenticated receipts uses `IMPORTED -> SCORED`. Every arrow is a
write-ahead journal phase: recovery resumes at the first uncommitted action, may re-run only the
explicitly idempotent absence/cleanup probe for the current phase, and never re-runs model dispatch,
snapshot creation after a committed snapshot, import after a committed import attestation, or any
scorer after a committed scorer attestation. It never transitions backward or re-enters `CLOSING`.
An empty submission with a complete authenticated status/seal and zero receipts is always the
non-delivery branch with its existing non-UNKNOWN G0; it is never “incomplete” infrastructure.
Export or cell census failure transitions from `ALLOCATED` or `PREFLIGHTED` through `CLOSING` to
fsynced `UNKNOWN_EVIDENCED -> DESTROYED`; dispatch is forbidden. Every terminal event enters the
same journal-driven close dispatcher before its evidence branch. It executes all applicable ordered
production-close actions and idempotent absence/cleanup probes based on recorded provisioning state,
including UID/process termination, Redis ACL disable/client-kill/prefix-user deletion, census, and
descriptor-safe root deletion; unavailable Git status/scoring is recorded as not-applicable rather
than bypassing cleanup.

The control plane has a read-only worktree, scrubbed allowlisted environment, provider/bus egress
only, no Git metadata/binary/config, and no host inheritance. The tool plane has a read-write
worktree, no network including loopback, no sibling process control, no Git metadata/binary, and only
the Git shim. The Git service is uncredentialed, deny-default, no-network, and sees only fixed Git
and worktree paths. All Python-capable planes set `PYTHONDONTWRITEBYTECODE=1`.

Generated Seatbelt profiles are canonical artefacts, not inline string fragments. Positive runtime
probes and negative network/Mach/process/filesystem probes are tests and readiness evidence.

## 5. Git service, close, and importer

The shim exposes only structured `status`, `add`, and `commit` RPCs. Before allocation, controller
limits are checked and enforced independently on every incoming request. The transport is exactly a
four-byte unsigned big-endian payload length followed by that many canonical-JSON bytes. Rate is
checked before reading the header; the fixed header is read first and lengths over
`max_frame_bytes` are rejected before payload allocation/receive. The exact-length body is parsed
incrementally; path byte/component/count limits are enforced before copying or
allocating each dynamic field, at the manifest-pinned constants: frame bytes `1048576`, canonical UTF-8/NFC path
bytes `4096`, components per path `256`, component bytes `255`, paths per request `1024`, concurrent
in-flight requests `8`, and status token bucket `4/second` with burst `8`. Gate 12 asserts these exact
values and boundary cases; runtime or post-hoc widening is forbidden.
It rejects `-c`, every caller-supplied environment/config override, arbitrary subcommands/raw
packets, any argument
starting `-`, and every path outside the immutable controller task allowlist. Internal Git path lists
always follow `--`; repository config, hooks, aliases, replacements, helpers, filters, attributes,
alternates, and worktree `.git` pointers are never consulted.
Path bytes must be valid canonical UTF-8/NFC and reject NUL/control bytes, empty/`.`/`..` components,
absolute roots, backslashes or any separator other than `/`, invalid encodings, and component/depth
limit overflow. The service uses a controller-built fixed environment and fixed absolute `GIT_DIR`
and `GIT_WORK_TREE`. It never calls `git add` with a model pathname: it hashes the still-open
validated descriptor, writes the resulting blob object into the fixed Git object database, and
stages that OID/mode through fixed plumbing without re-resolution.
Staging opens canonical UTF-8/NFC paths descriptor-relatively with no-follow semantics, explicitly
rejects hardlinks (`st_nlink != 1`), devices, invalid types, escaping links, and options, and proves
the opened descriptor remains beneath the fixed worktree before and after hashing. It updates the
index without re-resolving a model-controlled name. Gate 12 includes a hardlink-staging denial
tripwire. Hashing, staging, tree construction, and commit enforce per-file,
aggregate-byte, file-count, CPU, memory, wall-time, object-count, and disk limits. Status and its
filesystem-digest walk enforce CPU, memory, wall-time, maximum
traversal-depth, concurrently-open-descriptor, and response-size limits before and during work.

Each scored cell starts its dedicated daemon with `AGENT_AUTO_COMMIT=0` (the existing daemon-level
`--no-auto-commit` equivalent); no nonexistent per-request dispatcher flag is assumed. The cell runtime requires the model to
commit through the shim. In scored mode, `Bridge.process_request` selects a receipt-only completion
verifier and must never call the existing host `completion_gate`, orchestrator-commit, rescue commit,
or any controller/bridge `git -C <cell>` path. Empty receipts, dirty status, or a model-owned
inconsistent seal preserve a non-UNKNOWN G0 and produce G2 `not-delivered` plus the frozen
`NOT_SCORED` cascade. Launch/supervisor/instrumentation failure produces the authoritative G0
`UNKNOWN` cascade instead; it is never overwritten by the empty rule. There is no host-Git fallback.
Readiness contains tripwires that fail if the old completion gate, auto-
commit, or host Git touches scored cell metadata.

The implementation has one production close function used by scored cells and preflight:

1. stop new tools and make Git RPC status-only;
2. drain in-flight RPC;
3. SIGKILL and prove empty tool/control UIDs;
4. idempotently attempt disable, active-client kill, prefix empty/delete, and user delete using the
   controller-minted intended identity on every exit, tolerating only authenticated not-found
   results. Journal each provisioning side effect before the next begins, then prove the retired
   credential fails and both prefix/user are absent. `ACL_NOT_PROVISIONED` never skips cleanup probes;
5. if the journal proves the Git service was started and remains reachable, obtain final status,
   fsync receipts, seal and verify completion. If it was never started or is unreachable, record a
   bounded unavailable-status enum and continue cleanup without raising; classification follows the
   authenticated failure/provisioning evidence;
6. kill and prove empty the Git-service UID;
7. prove the controller's complete cell process-session ledger and an independent process census are
   empty, including double-fork/`setsid` descendants. If the journal proves the cell clone exists,
   create the descriptor-held immutable snapshot; otherwise record a bounded clone-absent enum and
   continue classification/cleanup without opening a path or raising;
8. first classify every authentication, version-pin, seal/status/verifier, provisioning, and
   controller result available before import. Any independent infrastructure fault takes the
   authoritative G0-G7 `UNKNOWN` cascade regardless of a budget record. Otherwise an authenticated,
   fsynced model-budget record always sets provisional G0 `FAIL` and takes `BUDGET_FAILED`; if its
   owning operation directly caused an incomplete seal, all available receipt/version/auth evidence
   must still be valid. A verified clean non-empty receipt seal
   still proceeds to import and, after successful graph
   attestation, scores with G2 `agent-delivered`; an empty/incomplete/non-deliverable seal sets
   public G2 `not-delivered`, G1/G3-G7 `NOT_SCORED`, and forbids importer/scorer.
   Otherwise set/preserve the
   authoritative G0-G7 `UNKNOWN` cascade for unauthenticated or incomplete
   seal/receipt material, since those are controller/auth/verifier failures. Otherwise, if receipts
   are empty or an authenticated model-owned seal is dirty or chain-inconsistent,
   emit G2 `not-delivered` and the frozen `NOT_SCORED` evidence branch with no importer/scorer;
9. otherwise import, attest, and score only a verified non-empty seal through trusted read-only
   materializations. At each phase, an importer/fsck/materialization/post-import-attestation or
   scorer-launch/supervisor infrastructure failure replaces any provisional ordinary or budget
   classification with authoritative G0-G7 `UNKNOWN` and stops the run;
10. after evidence is durable and all cell UIDs/process sessions are proven empty, delete the
    disposable cell root by descriptor-relative, no-follow traversal. Controller cleanup must never
    call `git worktree remove`, follow hostile worktree metadata, or run `git -C <cell>`; readiness
    tripwires those legacy paths.

The importer performs one iterative descriptor-held bounded pass into an importer-owned spool,
using a bounded explicit queue and bounded open-descriptor set. It accepts only canonical
loose objects `objects/[0-9a-f]{2}/[0-9a-f]{38}`, pack/index files
`objects/pack/pack-[0-9a-f]{40}.{pack,idx}`, and the single controller-named candidate ref. Every
other name/type is rejected. It walks without following links; accepts only regular files with link
count exactly one; rechecks type, inode, declared/actual size, and content digest before and after
each copy. Before every allocation/write it incrementally enforces file count, declared/actual
bytes, depth, open descriptors, memory, disk, and wall time; overflow destroys the spool and fails
closed. Each accepted source descriptor is copied before close/advance, and no model-controlled
pathname is reopened. Every loose object and every reconstructed packed object must hash to its
declared OID after copy/reconstruction. It verifies the first parent is the fixture, every subsequent parent is the
prior receipted commit, parsed commit/tree values equal receipts, and HEAD equals the final receipt;
uses no cell config; runs bounded `git fsck --strict`; independently walks parent/child parsed trees
to derive exact changed paths, requires equality with receipt paths, then checks the controller
allowlist; rejects gitlinks/escaping links; and emits a content-addressed bundle.
Controller Git uses a fixed allowlisted environment, system/global config disabled,
`core.hooksPath=/dev/null`, fixed protocol/file settings, and no repository-controlled config,
hooks, fsmonitor, SSH commands, helpers, filters, attributes, alternates, replacements, aliases, or
environment overrides.
The importer reproduces the controller-pinned canonical tree-materialization digest for every
receipt. Its sandbox enforces CPU, memory, wall-time, disk-byte, object-count, pack-size,
decompression-ratio, tree-depth, and commit-count limits before and during parsing; any limit or
unknown version fails closed.
For overlapping model-controlled dimensions, Git service acceptance ceilings equal or are stricter
than the manifest-pinned importer ceilings, and readiness asserts the inequality. A failure of an
importer-only integrity/parser limit after a consistent seal remains infrastructure `UNKNOWN`, as
required by the frozen classifier; it is never relabeled model non-delivery.

## 6. Scoring and gate truth

G1 uses three one-shot UIDs: `keyed-runner`, `broker`, `submitted-program`. Expected results and
battery plaintext never leave the keyed runner. G4 uses an entirely keyless three-UID topology:
`coordinator`, `suite-runner/broker`, `submitted-code`; it re-runs pinned public suites only after
import for every receipted OID. Every exit kills, reaps, and proves the applicable UIDs empty before
persisting results.

Implement the frozen classifications in this precedence order and test overlaps explicitly:

- first, reject any independent provisioning, controller, authentication, version-pin,
  seal/status/verifier, importer, fsck, materialization, or post-import-attestation infrastructure
  failure as authoritative G0-G7
  `UNKNOWN`, regardless of a coexisting budget record;
- otherwise, an authenticated, fsynced controller-sealed `MODEL_BUDGET_EXCEEDED` record for a tool command,
  controller ingress, or Git-service `status|hash|stage|tree|commit` operation makes the owning model
  operation G0 `FAIL` even if final status/seal is consequently incomplete; it cannot be
  overwritten by the incomplete-seal infrastructure rule;
- otherwise, infrastructure G0 `UNKNOWN` forces authoritative G1-G7 `UNKNOWN` for that attempt; no
  downstream model score, PASS/FAIL, or delivery outcome may coexist with it;
- otherwise G0 is the actual terminal completed/failed/timeout dispatch state;
- only when G0 is not `UNKNOWN`, empty receipts preserve that G0, set G2 `not-delivered`, and set
  G1/G3-G7 `NOT_SCORED`; the empty rule never overwrites an UNKNOWN cascade;
- when G0 is not `UNKNOWN`, a consistent non-empty seal whose ordered receipts are all authenticated
  and whose successfully attested imported graph contains every receipted OID sets G2
  `agent-delivered`; every other
  non-infrastructure delivery outcome is `not-delivered`. When G0 is non-UNKNOWN, the closed public
  G2 delivery enum is exactly `agent-delivered | not-delivered`; authoritative infrastructure
  cascade adds the separate value `UNKNOWN`. `DELIVERED`, `RESCUED`, `NOT-DELIVERED`, and state-machine labels
  are forbidden as public G2 values;
- whenever G2 is `not-delivered`, regardless of receipt count, G1/G3-G7 are `NOT_SCORED`, no
  importer or scorer is entered, and no fixture-tip or host-Git surrogate may supply a result;
- scored G2-G4 consume only authenticated receipts, the sealed completion record, imported graph,
  and scorer attestations. They must not call the legacy host `completion_gate`, inspect a cell
  worktree with host Git, or derive delivery/correctness from controller-side working-tree state;
- importer failure after a consistent seal is infrastructure `UNKNOWN` and stops the run;
- model-authored resource exhaustion is charged only at its owning G0/G1 boundary;
- a model tool command, controller-ingress operation, or Git-service
  `status|hash|stage|tree|commit` budget breach is G0 `FAIL`;
  model-authored execution timeout/resource
  limit inside an otherwise healthy scorer is G1 `FAIL`; launch/supervisor failures are `UNKNOWN`;
- bytecode already in the imported submitted tree or created/loaded/executed by G1
  `submitted-program` or G4 `submitted-code` is model G5 regardless of directory. Bytecode from a
  pinned suite mount or created/loaded by G1 `keyed-runner`/`broker` or G4
  `coordinator`/`suite-runner/broker` after verified clean materialization is infrastructure
  `UNKNOWN`; location alone never assigns blame;
- if bytecode is detected but creator/loader/executor attribution cannot be proven deterministically
  from the role instrumentation, classify the affected attempt as infrastructure `UNKNOWN`;
- before any G1/G4 result is accepted, instrumentation plus a descriptor-safe scan must prove no
  `.pyc`/`__pycache__` was present, created, loaded, or executed anywhere in the whole sandbox,
  including scratch/temp. Any detection fails closed before scoring, applies the role attribution
  above, and model-attributable bytecode also fails the affected execution gate. Caches are never
  ignored;
- G4 requires imported earlier-FAIL/later-PASS receipts for the pinned suite; otherwise
  `NOT_DEMONSTRATED`, never correctness FAIL.

Static public diagnostics and quarantine destruction are covered by adversarial canary tests.
Raw scorer stdout/stderr, assertions, tracebacks, and crash artefacts terminate in unlinked,
descriptor-only write sinks inside the existing scorer sandbox. The launcher retains no readable
descriptor or pathname grant and may return only the fixed bounded result schema over a distinct
authenticated channel. The trusted in-sandbox encoder owns schema validation and canary scanning;
no seventh scorer role is introduced. Tests must prove the controller cannot read the quarantine,
the public result still crosses, and every sink is destroyed after success, failure, timeout, and
cancellation.

Every authoritative attempt row includes `failure_category` with exactly
`none | model-implementation | protocol-import-infrastructure | other-infrastructure`.
Protocol/import covers malformed/truncated/version- or identity-mismatched boundary frames and
receipt/import/integrity/attestation IPC failures; it always produces the existing authoritative
G0-G7 `UNKNOWN` cascade. Model-authored execution inside a healthy evidenced boundary is
`model-implementation`; provider/bus/daemon/host/ACL/sandbox/scorer-supervisor and ambiguous faults
are `other-infrastructure`. Concrete causes remain private quarantined evidence.

## 7. Readiness implementation

`scripts/seat-preflight --strict` gains a bakeoff manifest mode. It instantiates separate real
representative cells per arm for each required success, empty, dirty, and hostile/failure fixture;
no cell is reused across close branches. It emits value-free JSON. The aggregate
`scripts/implbench preflight --manifest ...` independently runs all fourteen frozen gates:

1. Open Interpreter protocol/tool interception/retirement;
2. declared bench environment from fresh sync;
3. concurrency hard-refusal except `1`;
4. independently verified model/harness/control provenance. Every arm must return a provider- or
   harness-runtime acknowledgement, independent of request/config echo, for effective reasoning and
   every exposed generation/runtime control. Gate 4 rejects echo, `UNKNOWN`, drift, material
   mismatch, or unsupported asymmetry for any control, including unequal effective reasoning,
   within a pair before calibration or scoring. Requested and effective reasoning must both be
   exactly `medium`; each scored cell repeats the same comparison;
5. cell/attempt identity everywhere;
6. immutable schedule/manifest;
7. observed telemetry without invention. Gate 7 validates start/end timestamps, derived wall time,
   terminal status, retry count, tool-call count, and concrete provider/engine error evidence against
   live harness observations; it rejects absent, invented, unbounded, discarded, or publicly leaked
   error diagnostics;
8. G0/infrastructure classification;
   it includes a live tool-plane resource-limit fixture producing a controller-authored, fsynced
   `operation=tool` record (and no Git-service candidate) whose owning operation directly yields an
   authenticated empty/incomplete/non-deliverable seal, and proves G0
   `FAIL`, G2 `not-delivered`, G1/G3-G7 `NOT_SCORED`, and zero importer/scorer entry;
9. per-cell UID/home/config/credential/runtime cleanup. Before any representative daemon starts,
   prove credentials previously pasted into chat were rotated; replacements come only from
   controller-owned mode-0600 seat env files outside the active checkout or an external
   command-backed credential source. Freeze and digest non-secret config, forbid live env files from
   the active checkout, and prove the tool plane cannot read the config or credential source. The
   scoring controller is the only process/environment allowed to hold `IMPLBENCH_BATTERY_KEY`;
   live probes prove its name and value are absent from dispatch, daemon, engine-control, tool,
   Git-service, importer-submitted-code, G4, logs, argv, mounts, and evidence surfaces;
10. matched capability positive/negative probes, including a model-invoked tool shell proving all
    provider credential names/values absent and denying every network destination (`127.0.0.1`,
    `::1`, bridge/Redis, and external egress), plus a separate control-plane probe allowing only the
    configured provider and seat-bus endpoint classes;
11. G1/G4 scorer isolation and confidentiality attacks, including host-file, controller-secret,
    network, fork-bomb, post-exit persistence, hidden-test introspection, and output-exfiltration
    attempts without weakening the real battery. Gate 11 separately proves the keyed-runner receives
    `IMPLBENCH_BATTERY_KEY` through its controller-only inherited-descriptor boundary while broker and
    submitted-program environments/files/descriptors cannot recover its name or value;
12. Git shim/importer hostile metadata/object/path attacks, `files` backend enforcement, `reftable`
    rejection, canonical `realpath` equality for every allowed Git/worktree/fixture root, descriptor
    identity and hash accounting, complete worktree accounting, and tripwires proving host
    completion/auto-commit/Git never touches scored metadata. It builds the production quarantine
    export/clone. Its pre-clone census scans all export refs and the full export ODB, and its
    post-clone census asserts the clone's complete object set equals the transitive closure of the
    pinned base+fixture tips. Two independent hostile exports—one with a prior-result ref and one
    with an unreachable dangling prior-result object—must each fail at the export-stage census with
    a census evidence digest before clone transfer. After a clean export census and clone, two
    independent cell-only poisons inject an extra prior-result ref or unreachable prior-result
    object into the cell; each must fail the post-clone census, emit its census evidence digest, and
    prevent dispatch/import. Every census-failure fixture must also prove the close dispatcher leaves
    all applicable UIDs/processes, ACL user/prefix/clients, and cell root absent. Gate 12 also checks
    every overlapping manifest-pinned dimension satisfies
    `git_service_acceptance_ceiling <= importer_acceptance_ceiling`, exercising equality and strict
    inequality boundaries;
13. exact scored-path seat preflight including a control-plane profile proof denying the canonical
    cell Git directory, `.git` pointers, every host/bundled Git executable, and Git
    config/environment bypass, followed by the production close and full Redis ACL lifecycle;
    it must independently execute dirty-empty-receipt, clean-empty-receipt, clean non-empty-receipt, and hostile failed-
    seal branches and validate their
    authoritative records/postconditions without recomputing G0/G2. For every separate
    representative cell, Gate 13 also requires all frozen seat-scoped criteria: ping only its
    preflight-only cell-suffixed ID and verify the registry path equals its disposable clone; run an
    exact-text no-tool turn; run a read/edit/test/commit smoke through
    `agent-dispatch --worktree ... --worktree-cleanup keep`; verify configured model/provider/harness
    and engine version independently of prose; capture independent reasoning/control acknowledgements
    during exact-text; prove the base checkout byte-clean and process retired; prove
    `IMPLBENCH_BATTERY_KEY` absent from every seat boundary; prove the per-cell home starts empty and
    is destroyed with the runtime; deny base/sibling/evidence reads and tool-plane network/credential
    access; and prove the control plane reaches only configured provider and bus endpoint classes.
    PASS requires successful ACL
    provisioning, namespace empty before dispatch, cross-prefix read/write denial, namespace empty
    after close, user/prefix deletion, and retired authentication failure; endpoint reachability is
    not evidence for any namespace property;
14. controller-owned aggregate repetition that treats the seat JSON as non-authoritative. For each
    aggregate-owned representative cell it provisions one fresh ACL lifecycle, runs live pre-empty
    and cross-prefix denial probes, invokes the same normative production close, and live-verifies
    post-empty, user/prefix deletion, and retired-authentication failure. It independently executes
    all close branches, including an explicit `receipts=[]`, `dirty=true` fixture that must seal,
    preserve non-UNKNOWN G0, set G2 `not-delivered`, set G1/G3-G7 `NOT_SCORED`, and prove zero
    importer/scorer entry, plus an explicit complete `receipts=[]`, `dirty=false` fixture with the
    identical G2/NOT_SCORED/no-import postconditions. It records authenticated evidence digests, then runs validate and
    known-good Codex calibration. It neither reuses nor re-tears-down a seat-preflight cell and
    defines no second teardown algorithm. It also injects a missing/mismatched digest-version or
    status-verifier infrastructure failure and proves fsynced authoritative G0-G7 `UNKNOWN` rows
    survive `UNKNOWN_EVIDENCED -> DESTROYED` without empty-rule overwrite. It independently runs the
    hostile scorer probe from gate 11. Separately it uses live oversized-frame and rate breaches to
    prove controller-authored ingress records, and is parameterized over every operation
    `tool|ingress|status|hash|stage|tree|commit`. For complete clean non-empty seals it proves G0
    `FAIL`, G2 `agent-delivered`, and import/scoring; for empty/incomplete/non-deliverable seals it
    proves G0 `FAIL`, G2 `not-delivered`, G1/G3-G7 `NOT_SCORED`, and zero importer/scorer. Neither
    case permits incomplete-seal UNKNOWN overwrite, and both prove UID/ACL/root cleanup. It also
    combines every operation with importer/fsck/materialization/post-import-attestation failure and
    proves authoritative G0-G7 `UNKNOWN`; combines a budget record with missing/mismatched digest
    version and other seal-verifier faults and proves the same cascade. Negative cases for
    missing authentication/nonce, bad operation/reason enum, unknown budget dimension, and
    unfsynced records must produce infrastructure UNKNOWN and never model FAIL. A standalone,
    non-budget, receipt-consistent seal followed by each importer/fsck/materialization/attestation
    failure must persist authoritative G0-G7 `UNKNOWN` through `UNKNOWN_EVIDENCED -> DESTROYED`.
    Gate 14 also injects model-attributable, infrastructure-attributable, and unprovably attributed
    bytecode creation/load/execute traces across the six scorer roles and proves the frozen G5 versus
    infrastructure-UNKNOWN outcomes. It also runs the zero-mutation
    prune canary defined in section 9. Gate
    14 cannot PASS without all of these proofs.

Each sub-gate has `{gate_id, status, evidence_digest, started_at, ended_at}` where status is only
PASS/FAIL/UNKNOWN. Aggregate PASS requires fourteen PASS values and a clean controller checkout.

## 8. CLI and execution phases

Extend `scripts/implbench` with:

```text
validate
preflight --manifest PATH
calibrate --manifest PATH --seat ID
pilot --manifest PATH
run --manifest PATH
report --evidence PATH
prune --before DATE --evidence-root ABSOLUTE_PATH
```

`pilot` runs repetition 1 in frozen order and may append only infrastructure-repair attempts. Full
`run` requires a pilot package that validates and a sealed pilot record/content digest in the
append-only journal; it does not require all model gates to PASS. That
digest binds the complete repetition-1 manifest, redacted effective configs, live run/result ref
names and tips, and all append-only rows through the pilot journal tail. Before repetition 2, `run`
recomputes and matches that digest, retains all model-attributable FAIL/stop outcomes, permits only
the frozen append-only infrastructure-UNKNOWN repair attempts, and then continues
repetitions 2-4 without rewriting pilot data,
runs one cell at a time. The schedule seed is exactly 32 bytes encoded as 64 lowercase hex
characters. Starting from task IDs sorted by their UTF-8 byte strings, generate the canonical task
order with descending Fisher-Yates. For every random-word attempt, compute
`digest = SHA-256(UTF8("implbench-schedule-v1") || byte(0x00) || seed_bytes || uint64_be(counter))`,
increment `counter`, and interpret only `digest[0:8]` as one unsigned 64-bit big-endian word. For
bound `i+1`, reject words at or above `floor(2^64/(i+1))*(i+1)`, generating a new counter/digest for
each retry, and use `word mod (i+1)`. Counter starts at zero. The schedule is frozen before dispatch:
within each pair/task Pi runs first
when `(repetition + task_index)` is even and Open Interpreter first when odd, where `task_index` is
the zero-based position in the seeded canonical task list before any even-repetition reversal; pair order is
GLM→Kimi for repetitions 1 and 3 and Kimi→GLM for 2 and 4; task order comes from the recorded seed
and reverses on even repetitions. The unique linear nesting is repetition `1..4`, then the applicable
pair order, then the applicable task order, then the two-arm order. The Pi/Open Interpreter arm map
is `glm-pi/glm-zcode` for GLM and `kimi-pi/kimi-cli` for Kimi. Manifest validation independently
expands all 128 cells in that exact nesting, assigns zero-based schedule indices and derived IDs,
and requires byte-for-byte equality with the stored schedule before dispatch. Pilot, run, and every scored controller path stop the affected
pair immediately before further dispatch on wrong model/provider/harness/engine version, context
reuse or any resume/fork/continuation/warm-thread path, hidden-key exposure including any
`IMPLBENCH_BATTERY_KEY` seat-boundary presence, paired fixture-SHA mismatch, writes outside the assigned worktree,
base/sibling drift, malformed or missing NDJSON, discarded concrete provider error, reasoning that
is unknown, unequal, non-`medium`, or echo-only, or three infrastructure failures with the same cause. Provider
and bridge failures remain `UNKNOWN` until diagnosed and are never silently retried into a scored
success; stopped and model-attributable outcomes remain append-only. `report` and
`final-comparison.md` implement frozen analysis schema `pair-analysis-v1` separately for GLM and
Kimi: the per-task/per-repetition G0-G7 grid for both arms; G1 paired wins/losses/ties by task plus
an exact paired sign test for every nonzero non-tied sample, labeled `underpowered` below eight
non-tied pairs; every G3/G5/G6/G7 regression as a named finding; separate delivery and TDD shapes;
successful-cell median/p95 wall time with failure counts; repeated task-family asymmetries; and
within-arm variance. The match key is `(model family, task, repetition)` with identical fixture and
config pins. `PASS` beats `FAIL`; equal G1 values tie. If either arm is
`UNKNOWN|NOT_SCORED|missing|pin-mismatched`, the pair is non-scoreable, never imputed, and reported
by arm and `failure_category`. Attempt zero is selected when fully authoritative and scoreable. A
replacement is eligible only along a contiguous append-only repair chain: after authoritative G0
`UNKNOWN` classified `protocol-import-infrastructure|other-infrastructure` and before redispatch, a
controller-authenticated fsynced authorization binds prior ID/number, next ID/number (`prior + 1`),
failure category, quarantined cause digest, and controller sequence. Select the lowest fully
authoritative scoreable attempt number reachable through that chain; a missing/late link, model
failure, or gate `FAIL` cannot authorize replacement, and all attempts remain append-only.
Per-task aggregation reports four-repetition win/tie/non-scoreable counts; repeatable direction
requires at least three wins and zero reverse wins. The primary analysis reports a two-sided exact
sign test plus a central equal-tail 95% exact Clopper-Pearson interval over scoreable non-ties. For
OI wins `w`, Pi wins `l`, and `n=w+l`, p is
`min(1, 2*BinomCDF(min(w,l); n, 0.5))`; Clopper-Pearson uses beta quantiles with boundary values 0/1,
and `n=0` reports p `1`, interval `[0,1]`, and `underpowered`. Fewer than eight non-ties is
`underpowered`. The hard-floor domain is every deterministically selected authoritative attempt for
the model pair, including G1 ties and attempts outside the sign-test sample; an arm is clear exactly
when none has G3/G5/G6/G7 `FAIL`, and a shared failure clears neither arm. An arm wins only with at
least eight non-ties, more wins, `p <= 0.05`, and a clear hard floor. All other primary outcomes are
`tie`, while repeatable opposing task-family directions remain `mixed/decorrelated`. Each pair
receives exactly one closed evidence shape:
`openinterpreter-dominated|operationally-equivalent|openinterpreter-adds-capability|mixed-decorrelated`,
with corpus-limited language and no automatic promotion. Manifest validation pins this rule ID and
report validation rejects absent fields, rankings, or composite scores. Before pilot, calibration
runs the hermetic suite, adversarial validation, a known-good Codex attempt, and one unscored task
through all four new seats using the scored runtime path. Pilot sealing never creates `git-refs.txt`; that final index remains absent
until the entire run is closed.

## 9. Evidence package

The controller-owned evidence root is excluded from every seat/sandbox mount and contains exactly:

```text
manifest.json
cells.ndjson
results/<run-id>.ndjson
reports/<run-id>.json
seat-config-redacted/
preflight/
git-refs.txt
worktree-accounting.txt
final-comparison.md
```

The manifest stores secret names only. NDJSON rows include immutable cell/attempt identity,
fixture/config digests, controls/provenance, prior-record digest, and authoritative gate values.
Preserve run/result refs indefinitely within this implementation. Raw runner output, tracebacks,
assertions, battery-derived text, dynamic diagnostics, and crash artefacts never enter the package.
In-progress startup/execution resume validates the immutable manifest, append-only journal and live
refs and requires final `git-refs.txt` to be absent. A present partial, malformed, or unsealed file
refuses both in-progress and closed-run operation; atomic seal uses a distinct controller-only
temporary filename that can never satisfy validation. Closed-run reporting requires and validates
the sealed final `git-refs.txt`, complete layout, schemas, journal tail, manifest digest, referenced
artefacts, and exact Git refs. Calling `preflight`, `calibrate`, `pilot`, `run`, execution-resume, or
any other package-mutating command on a package with a valid sealed final index fails closed. Only
read-only `validate` and `report` remain as package commands; `prune` may consume the closed package
solely through its external evidence-root protection scan and never mutates the package.
Every bakeoff `run_id` starts with `oi-pi-bakeoff-`; each cell pins exactly
`refs/implbench/runs/<run_id>/<cell_id>/<attempt_id>` before dispatch and, after successful import,
`refs/implbench/results/<run_id>/<cell_id>/<attempt_id>`. The ref helper/API is migrated away from
the legacy task-only shape. The existing `git-refs.txt` evidence artefact is a versioned,
canonically sorted final ref index containing the manifest digest, journal-tail digest, and every
exact run/result ref and OID. Every digest field carries its required digest-version field; absent
or unknown versions refuse report, resume, validation, and prune. It is written once and sealed only
after the run closes; it is not a
second manifest and cannot alter pre-run pins. Pruning result refs or
deleting the evidence package is out of scope. The existing date-based `scripts/implbench prune`
must classify protected refs before mutation and never mutate a bakeoff run ID or manifest-owned ref;
there is no bakeoff prune approval or deletion path in this implementation. Readiness seeds an old
ordinary ref beside old `oi-pi-bakeoff-*` and manifest-owned refs, invokes the date-prune path, and
proves protected refs and their reachable objects remain byte-identical while an eligible ordinary
ref is deleted successfully. Before selecting or
mutating candidates, prune requires the controller to supply a canonical absolute
`--evidence-root`; absence refuses the command. It validates that path against each applicable
`manifest-v2` and controller configuration, then takes the union of closed ref sets from every
cryptographically and canonically validated `git-refs.txt` plus its bound `manifest-v2` beneath that
root, plus exact live refs derived from every valid in-progress `manifest-v2` and authenticated
append-only journal. A ref is protected if its
run ID starts with `oi-pi-bakeoff-`, its refname starts with
`refs/implbench/runs/oi-pi-bakeoff-` or `refs/implbench/results/oi-pi-bakeoff-`, or it belongs to that
union. Run-ID extraction is a total parser: only
`refs/implbench/{runs|results}/<run_id>/...` yields a run ID; every nonconforming arbitrary ref
(including `refs/heads/*`) yields `None` without throwing and simply fails the prefix predicate.
Missing, unreadable,
invalid, or concurrently changing evidence roots/manifests make prune exit nonzero before mutation;
the legacy `--before` surface without `--evidence-root` always refuses. The canary
separately proves (a) a date-eligible non-prefix ref protected only by a validated final ref index,
(b) a date-eligible `oi-pi-bakeoff-*` ref present in zero validated final indices and protected only
by its prefix, (c) a synthetic date-eligible non-prefix active-run ref present only in a valid
in-progress manifest/journal and zero final indices, and (d)
missing/invalid evidence-root refusal;
the protected cases leave byte-identical refs and reachable objects, while ordinary cleanup remains
available. Tests replace the legacy no-root success expectation with mandatory-root refusal and all
four production-CLI canaries (a)-(d).

## 10. Acceptance evidence

Before setting `execution_base_sha`, all of the following must be committed and green from a clean
checkout:

- new/updated unit, integration, hostile-fixture, and macOS sandbox tests;
- fresh-sync `scripts/implbench --help` and `validate`;
- aggregate gate-14 dirty-empty branch and mixed old-ref prune canary, proving protected refs and
  objects byte-identical, ordinary deletion successful, and missing/invalid-root refusal before
  setting `execution_base_sha`;
- Open Interpreter exact-text and tool interception smoke without scored tasks;
- strict preflight for all four arms;
- fourteen-gate aggregate manifest;
- known-good Codex calibration clearing every cluster;
- `git diff --check`, doc recipe/drift checks, targeted tests, then the full repository suite.

Any gate that cannot be proved on this Mac mini remains FAIL/UNKNOWN. The implementation may not
replace a frozen security boundary with documentation, a mock, or a weaker compatible path.

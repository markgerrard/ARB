# Pi vs Open Interpreter implementor bake-off — isolated-worktree design

**Status:** CONVERGED — zero verified P0/P1 in unanimous audited r24
`panel-oi-pi-design-20260713T200405Z-e7860d` and
`panel-oi-pi-design-r1-20260713T201310Z-b4be47`, plus
`panel-oi-pi-design-r2-20260713T202018Z-ea529c` and
`panel-oi-pi-design-r3-20260713T203132Z-c76c24` (GLM timed out in both), and
`panel-oi-pi-design-r4-20260714T032024Z-919665` (GLM operator-cancelled for latency), plus
`panel-oi-pi-design-r5-20260714T033825Z-d32fa0` (Codex/agy/Grok all `needs-changes/P1`). Every
round through r5 is audit-closed with `outcome=emitted`. Round r6
`panel-oi-pi-design-r6-20260714T034305Z-33e7a4` also closed `needs-changes` (Grok P0; Codex/agy
P1). Round r7 `panel-oi-pi-design-r7-20260714T034723Z-b5cfe2` closed `needs-changes` with P0
live-Git/control-plane escape findings. Round r8
`panel-oi-pi-design-r8-20260714T035231Z-c87f18` closed `needs-changes` with P0 completion/OID and
control-plane metadata gaps. Round r9
`panel-oi-pi-design-r9-20260714T035715Z-160b3a` closed `needs-changes` with confirmed P0 whole-cell
termination, object-integrity, completion-channel, and Git-service-confinement gaps. The current r9
security fold was re-panelled in r10
`panel-oi-pi-design-r10-20260714T040455Z-8d338b`, which closed `needs-changes/P1` on descriptor-stable
staging, receipt/completion binding, lifecycle ordering, process ownership, and control-plane Git
denial. Round r11 `panel-oi-pi-design-r11-20260714T040910Z-497768` closed `needs-changes/P1` on
mutator freeze, resource bounds, same-UID process protection, independently derived path sets,
digest pinning, and zero-commit/scorer transport contracts. The current r11 fold requires a clean
mandatory re-panel. No scored dispatches have run.
Round r12 `panel-oi-pi-design-r12-20260714T041319Z-f5168c` closed `needs-changes/P1` on version and
generation-control reproducibility, lifecycle/UID isolation, bounded RPC/status contracts, scorer
IPC, non-delivery scoring, and importer-failure attribution. The current r12 fold requires re-panel.
Round r13 `panel-oi-pi-design-r13-20260714T041723Z-f7c9fd` closed `needs-changes/P1` on exhaustive
control verification, completion ownership, UID-wide termination, environment/profile pins,
diagnostic leakage, filesystem metadata binding, and G4 non-delivery semantics.
Round r14 `panel-oi-pi-design-r14-20260714T042234Z-2037b9` closed `needs-changes/P1` on G4 evidence,
environment/inference freeze, scorer/importer lifecycle, control-plane and bus isolation, preflight
coverage, and completion digest binding. The current r14 fold requires re-panel.
Round r15 `panel-oi-pi-design-r15-20260714T042804Z-3a20f5` closed `needs-changes/P1` on sealed
post-import G4 evidence, suite pins, whole-worktree status semantics, bus probes, static diagnostics,
and traversal/copy budgets. The current r15 fold requires re-panel.
Round r16 `panel-oi-pi-design-r16-20260714T043210Z-6b690d` closed `needs-changes` with a P0
unsandboxed G4 execution path plus P1 suite/digest, bus-readiness, battery-freeze, deterministic
runtime, census, and schema gaps. The current r16 fold requires re-panel.
Round r17 `panel-oi-pi-design-r17-20260714T043611Z-0ab932` closed `needs-changes/P1` on G4 suite
schema and every-exit lifecycle, fail-closed bytecode, scored Redis ACL teardown, scrubbed controller
Git, and iterative census. The current r17 fold requires re-panel.
Round r18 `panel-oi-pi-design-r18-20260714T043932Z-a6ba8b` closed `needs-changes/P1` on suite-version
freeze/audit fields, keyless G4 role binding, bytecode attribution, short-chain G4 semantics, ordered
active-client ACL teardown, and single-pass importer snapshotting. The current r18 fold requires re-panel.
Round r19 `panel-oi-pi-design-r19-20260714T044315Z-07418a` closed `needs-changes/P1` on exact G4 role/kill
semantics, ACL disable ordering, whole-sandbox bytecode attribution, tree-digest version freeze,
zero-receipt import handling, and normative scored close ordering. The current r19 fold requires re-panel.
Round r20 `panel-oi-pi-design-r20-20260714T044704Z-6b294e` closed `needs-changes/P1` solely on
duplicated-surface contradictions in preflight ACL/import order, zero-receipt verification, and G4
role terminology. The current r20 consistency fold requires re-panel.
Round r21 `panel-oi-pi-design-r21-20260714T045002Z-59d0ca` closed `needs-changes/P1` on residual
duplicate ACL surfaces, six-role bytecode vocabulary, and the empty-receipt evidence branch. The
current r21 consistency fold requires re-panel.
Round r22 `panel-oi-pi-design-r22-20260714T045308Z-c8e688` closed `needs-changes/P1` on an open-ended
scorer UID alias and residual aggregate/section-7 close-outcome restatements. The current r22
consistency fold requires re-panel.
Round r23 `panel-oi-pi-design-r23-20260714T045601Z-39e48e` closed with Codex/Grok approvals and one agy
`needs-changes/P1` on residual `keyed-runner` alias wording and preflight-output phrasing. The r23
wording-only fold requires one final re-panel.
Round r24 `panel-oi-pi-design-r24-20260714T045929Z-c44fcd` closed `approve` with unanimous
Codex/agy/Grok `approve/none` and `outcome=emitted`. Design is frozen; implementation spec and plan
may refine execution detail but may not weaken or reinterpret these contracts. No scored dispatches
have run.
**world_at:** `2130fa45fd897a0e5c2a3e40f8aae1140b337b27`
**workflow:** B — rigorous parallel review + cold-Opus final; explicitly selected by Mark on
2026-07-13 before the first implementation dispatch.
**initial author:** inline warm orchestrator for design, spec, and plan; explicitly selected by
Mark on 2026-07-13. Anthropic-lineage reviewers are non-certifying for these authored stages.
**execution_base_sha:** unset until the readiness implementation in section 6 is merged and
verified; every scored arm will use that same immutable descendant commit.
**Scope:** two matched harness comparisons on the existing Implementor Bench corpus:
Pi vs Open Interpreter for GLM-5.2, and Pi vs Open Interpreter for Kimi K2.7.

## 1. Decision this experiment can make

This is two independent comparisons, not a four-seat leaderboard:

1. Does `glm-5.2` perform differently as an ARB implementor under Pi versus Open
   Interpreter's `zcode` harness?
2. Does `k2p7` perform differently as an ARB implementor under Pi versus Open
   Interpreter's `kimi-cli` harness?

The experiment measures the **deployed seat stack**: model, provider route, harness, tool
surface, bridge adapter, and completion machinery. It cannot claim a prompt-only causal effect.
That limitation is material for Kimi because the Pi provider uses an Anthropic Messages-shaped
route while Open Interpreter uses Chat Completions.

The output is evidence for a human routing decision. It must not automatically alter trust,
quorum, routing, or production seat configuration, and it must not emit a composite score.

## 2. Four pinned arms

| Pair | Arm | ARB engine | Provider/model | Harness | Required process isolation |
|---|---|---|---|---|---|
| GLM | `glm-pi` | `pi-sdk` | `zai/glm-5.2` | Pi | `BRIDGE_PI_RETIRE_AFTER_TURN=1`; fresh per-cell `PI_CODING_AGENT_DIR` |
| GLM | `glm-zcode` | `openinterpreter` | `zai-coding-plan` / `glm-5.2` | `zcode` | retire after every turn; fresh per-cell `INTERPRETER_HOME` |
| Kimi | `kimi-pi` | `pi-sdk` | `kimi-coding/k2p7` | Pi | `BRIDGE_PI_RETIRE_AFTER_TURN=1`; fresh per-cell `PI_CODING_AGENT_DIR` |
| Kimi | `kimi-cli` | `openinterpreter` | `kimi-for-coding` / `k2p7` | `kimi-cli` | retire after every turn; fresh per-cell `INTERPRETER_HOME` |

Proposed routable ID prefixes, subject to the bridge's normal derived-ID validation; each scored
cell appends a short immutable cell suffix so identity leases and daemon state cannot cross cells:

- `pi-sdk-agentredisbridge-bake-glm52`
- `interpreter-agentredisbridge-bake-glm52-zcode`
- `pi-sdk-agentredisbridge-bake-k2p7`
- `interpreter-agentredisbridge-bake-k2p7-kimicli`

All four arms use:

- the same benchmark-controller Git clone and immutable base commit;
- the same workspace-write capability ceiling;
- the same effective capability ceiling: read repository content, search/list, run shell commands,
  edit files, and write files inside the assigned worktree; neither arm may expose arbitrary or
  tool-command network access, browser, memory, sibling-worktree, base-checkout, or
  controller-evidence access; provider API egress is available only to the engine control plane,
  never a model-invoked tool process;
- reasoning/thinking level `medium` only when both arms in the pair report that level as effective;
  otherwise the pair is not scoreable;
- an exhaustive per-arm manifest of every provider/harness-exposed generation and runtime control,
  including temperature, top-p/top-k, seed, penalties, maximum output, stop behavior, tool choice and
  parallel-tool behavior, retry/backoff, and all timeouts. Unsupported and unknown states are
  explicit. Every control has requested/effective/verified-via fields; request/config echo and model
  prose are not verification. Any material unknown, unsupported asymmetry, or mismatch makes the
  pair non-scoreable;
- controller-pinned binary/content digests and version strings for ARB bridge/adapter, Pi SDK, Open
  Interpreter, `zcode`, `kimi-cli`, provider client libraries, Git, Seatbelt profile generator,
  importer, scorer, corpus, macOS build, Python/Node runtimes, and every generated Seatbelt profile
  and Mach allowlist blob. Calibration, pilot, every scored cell, and reruns verify the same pins;
- no role profile, project instruction file, optional skill pack, memory MCP, or unrelated
  extension;
- fresh model context and a fresh engine process for every scored cell;
- provider credentials supplied through an engine-only secret boundary, never argv, the repository,
  a benchmark fixture, the result artefact, or any model-invoked tool-command environment.

An exact tool-name or schema match is neither possible nor desirable: the point is to compare Pi's
native tool surface with the model-specific Open Interpreter harness surface. The matrix manifest
must record each arm's effective tools and map every tool to the capability classes above. A
pre-flight fixture must prove that every required class works and every prohibited class is denied.
If either surface cannot be constrained to the same classes, that model pair is not scoreable; the
design must not quietly widen one arm or claim matched capability.

The same rule applies to reasoning effort. The manifest records requested and effective reasoning
levels plus the authoritative acknowledgement source for each arm. A requested value that the
provider or harness silently ignores is not a match. `UNKNOWN` or unequal effective levels stop that
pair before scoring. `reasoning_effective` may be populated only from a provider or harness runtime
acknowledgement that is independent of the request/config value. Config inspection, request echo,
model prose, or copying `reasoning_requested` is not evidence; absent a runtime acknowledgement the
effective value is `UNKNOWN` and the pair is not scoreable.

## 3. Experimental unit and corpus

One **cell** is:

`pair × arm × task fixture SHA × repetition`

Use the committed Implementor Bench v1 corpus under `bench/implbench/fixtures/`:

| Cluster | Task |
|---|---|
| C1 | `c1-permissive-boundary` |
| C1 | `c1-token-bucket` |
| C2 | `c2-parser` |
| C3 | `c3-refactor` |
| C4 | `c4-rail` |
| C5 | `c5-artifact` |
| C6 | `c6-scope` |
| C7 | `c7-provenance` |

Run four repetitions. Total scored cells:

`2 model pairs × 2 arms × 8 tasks × 4 repetitions = 128 dispatches`

Four repetitions are required because previous ARB bake-offs showed large within-model variance.
One pass is a smoke test, not evidence of seat quality.

## 4. Isolation model

### 4.1 Dedicated benchmark clone

Do not run scored work from the user's active checkout. After the readiness changes in section 6
are merged and verified, record that commit as `execution_base_sha` and create a disposable,
controller-only clone pinned to it. That clone owns fixture/result refs and is never the workdir of a
benchmark seat.

`world_at` records the tree against which this design's observations were made. It is not the scored
base: it lacks the Open Interpreter engine and the required bench repairs.

```sh
BASE_SHA=<verified-readiness-integration-sha>
ARB_SRC_REPO=<absolute-clean-source-repo>
RUN_ROOT="/Users/Shared/arb-implbench/<run-id>"  # pre-created 0700; unique run id
test "$(git -C "$ARB_SRC_REPO" rev-parse HEAD)" = "$BASE_SHA"
test -z "$(git -C "$ARB_SRC_REPO" status --porcelain)"
git clone --no-local "$ARB_SRC_REPO" "$RUN_ROOT/repo"
git -C "$RUN_ROOT/repo" checkout --detach "$BASE_SHA"
test "$(git -C "$RUN_ROOT/repo" rev-parse HEAD)" = "$BASE_SHA"
test -z "$(git -C "$RUN_ROOT/repo" status --porcelain)"
```

The controller clone has its own Git object store. For each cell, the controller creates a second
disposable clone with its own object store, imports only the pinned base and that cell's fixture
commit, and launches that cell's bridge daemon with the cell clone as `AGENT_WORKDIR`. The seat clone
must contain no prior result refs or objects. After the seat terminates, the controller imports the
result commit back into the controller clone, pins the result ref, and destroys the cell clone. This
extra clone boundary is required because a worktree sharing the controller clone could enumerate
prior result refs and objects even if sibling working directories were hidden.

`ARB_SRC_REPO` is a controller-supplied, non-symlinked absolute path to a clean repository already
pinned at `BASE_SHA`; the manifest records its realpath and rejects drift. It is not inferred from
the current working directory and seats never receive it. `/Users/Shared/arb-implbench` is the
canonical host/sandbox root on macOS. The controller creates
each clone and linked worktree at the exact literal path later mounted into both runtime planes; it
must not create under `/tmp`, `/var/folders`, a symlinked path, or any host path that resolves to a
different physical name inside the sandbox. Preflight compares `realpath` inside and outside each
plane before Git is allowed to run.

### 4.2 Per-cell worktree

For every cell, the Implementor Bench must:

1. deterministically materialize the fixture into an orphan commit;
2. mint immutable `cell_id` and `attempt_id` values from pair, arm, task, repetition, schedule index,
   and attempt number;
3. pin it at `refs/implbench/runs/<run_id>/<cell_id>/<attempt_id>` before dispatch;
4. dispatch with `--worktree <unique-name> --worktree-base <fixture-sha>
   --worktree-cleanup keep`;
5. terminate the entire cell process session after the model turn ends, including the engine,
   bridge daemon, Git service, tool children, and descendants, and prove through the controller's
   process supervisor that zero cell PIDs remain before snapshot or import;
6. if and only if receipts are non-empty, pass inert Git objects/refs from the untrusted cell directory through the quarantined importer in
   section 4.4; verify and emit a content-addressed bundle; import that bundle into the clean
   controller clone; materialize the complete receipt-validated chain plus its pinned tip read-only
   for the scorer; run G1, G3, G5-G7 against that exact trusted tip and G4 against the same imported
   OID sequence; and pin the tip at
   `refs/implbench/results/<run_id>/<cell_id>/<attempt_id>`. Scoring the live cell worktree or any
   pre-import tree is forbidden;
7. remove the physical worktree and cell clone only after all evidence is durable.

The unique name must encode pair, arm, task, repetition, and a nonce. A cell may never reuse another
cell's worktree or conversation. The extra worktree inside the disposable cell clone is intentional:
the clone is the secrecy/object boundary, while the worktree exercises the same ARB isolation and
completion path used by real seats.

### 4.3 Session and home isolation

- Pi SDK: `BRIDGE_PI_RETIRE_AFTER_TURN=1` and a fresh empty `PI_CODING_AGENT_DIR` for every cell.
- Open Interpreter: a fresh empty `INTERPRETER_HOME` for every cell and engine retirement after
  every turn. The new engine must expose an Interpreter-specific retirement setting instead of
  relying silently on `BRIDGE_CODEX_RETIRE_AFTER_TURN`.
- The controller must create those directories after the prior cell ends, bind them only into that
  cell's confined runtime, and destroy them after evidence capture. A per-arm home is forbidden.
- Each cell gets a fresh bridge daemon with a cell-suffixed agent ID and only that cell's disposable
  clone as its workdir. The daemon is stopped before scoring and its identity is never reused.
- No resume, fork, continuation, or warm-thread reuse is allowed in scored runs.
- Provider auth may be shared as an external secret source, but session/config/state directories may
  not be shared between cells.

### 4.4 What a Git worktree does not prove

A worktree is collision isolation, not a security boundary. A capable process can still address an
absolute path outside its cwd unless its OS sandbox prevents that. Every scored cell therefore has
three explicit runtime planes:

1. an **engine control plane** containing the bridge, model harness, provider credential, and
   allowlisted provider and bus/Redis egress; it receives the worktree read-only and never mounts, reads,
   resolves, or writes the cell Git directory or worktree `.git` pointer. It also receives its
   ephemeral state home and daemon configuration through a control-plane-only secret mount or
   inherited descriptor. Its environment is rebuilt from an explicit controller allowlist and
   contains no inherited host variables beyond pinned runtime paths, `PYTHONDONTWRITEBYTECODE=1`, required provider secret
   descriptors, and named bridge settings. Its deny-default profile explicitly denies the fixed cell `GIT_DIR`, every
   worktree `.git` pointer, execution of host or bundled Git, and Git config/environment overrides;
2. a **tool plane** for every model-invoked shell or file tool, confined on macOS by a generated
   Seatbelt profile whose default is deny and which denies all networking, including host loopback,
   all Mach service lookup by default, and process execution outside an explicit non-Git tool
   allowlist. A frozen minimal Mach-service allowlist may be added only after a positive runtime
   probe proves it necessary and negative XPC probes prove it cannot access files, execute commands,
   resolve network names, or reach host services. It receives a scrubbed environment, the cell
   worktree mounted read-write with `.git` and nested gitdirs excluded and creation denied, no Git
   metadata, a narrow `git` shim socket, and `PYTHONDONTWRITEBYTECODE=1`. The tool plane cannot execute any real Git binary or
   address `/usr/bin/git`, `xcrun git`, bundled Git, or config/env bypasses. It also cannot read the
   engine config, provider credential, controller clone, active checkout, controller home, prior
   result objects, hidden-battery material, or evidence directory. Its profile also denies
   process-info, signals to other planes, task ports, debugging/tracing, and nonessential
   `sysctl-read`; hostile kill/attach/task-port probes against control and Git-service PIDs must fail;
3. an uncredentialed **Git service plane** under a distinct deny-default Seatbelt profile with no
   network, process inspection, engine/control secrets, controller or evidence mounts, and a
   controller-owned read-only minimal config/hooks view. Its only filesystem grants are its fixed
   Git directory, fixed worktree, sealed RPC descriptor, and bounded runtime libraries. It alone sees the
   cell Git metadata and implements a closed RPC vocabulary for `status`, `add <allowed paths>`, and
   `commit <bounded message>`. The shim cannot pass `-c`, environment overrides, arbitrary Git
   subcommands, arguments beginning with `-`, paths outside the controller's immutable per-task
   allowlist, or raw protocol packets; every internal Git path list follows `--`. RPC ingress is
   controller-bounded by frame bytes, canonical UTF-8/NFC path bytes, component count/length, paths
   per request, in-flight requests, and status rate. Paths reject NUL, control bytes, empty/dot/dotdot
   components, absolute roots, separators outside `/`, and invalid encodings without excluding valid
   corpus names. Before staging it
   walks every path by descriptor with no-follow semantics, rejects symlinked path components,
   hardlinks, devices and escaping links, and proves each regular file remains beneath the fixed
   worktree. It never passes a verified pathname to `git add`: it hashes bytes from the still-open
   validated descriptor and stages the resulting OID/mode through a fixed plumbing operation, so
   model-writable names are never re-resolved. The service refuses repository
   config, hooks, replacements, helpers, filters, attributes, and alternates regardless of cell
   contents and exits after the cell. It uses controller-fixed absolute `GIT_DIR` and
   `GIT_WORK_TREE` paths and never reads a worktree `.git` pointer. Every successful commit returns a
   candidate receipt containing cell/attempt IDs, fixture-root OID, ordered parent OIDs, commit OID,
   tree OID, observed changed paths, canonical tree-materialization digest and its version, resulting HEAD, and
   final dirtiness. Merge commits are forbidden, so ordered parents has exactly one element. The
   controller—not the service—assigns the monotonic sequence, binds it
   to a nonce-authenticated per-attempt channel, validates observed paths against its immutable
   allowlist, appends and fsyncs the receipt in an out-of-cell attempt log, and only then
   acknowledges success. Service output can never widen the controller-owned path policy.
   Descriptor hashing, staging, tree construction, and commits have controller-fixed per-file,
   aggregate-byte, file-count, CPU, memory, wall-time, object-count, and disk limits; exceeding any
   budget fails the cell as model-attributable G0 resource exhaustion. The same CPU/memory/time,
   maximum traversal depth, concurrently open descriptors, and response-size budgets apply to
   status, including its filesystem digest walk.

The cell clone and linked worktree use fixed canonical paths that are identical when the worktree is
created and when it is mounted into the Git-service plane. The service does not resolve a model-
writable `gitdir:` pointer. Preflight must prove the tool-plane shim can perform
`status`, allowed-path `add`, and `commit` through the shim and Git service while direct/raw Git,
planted metadata, option injection, symlink/hardlink staging, arbitrary subcommands, and path escape
are denied. No preflight may claim that ordinary host Git succeeds in the tool plane.

The harness integration must enforce that split at the tool-execution boundary; putting engine and
tool subprocesses in one ordinary container does not satisfy it. A process running as the
controller's ordinary host user is not sufficient. The benchmark also requires:

- scored task prompts explicitly require a shim commit and seats use `--no-auto-commit`; a missing
  commit receipt or dirty Git-service status is `not-delivered`, never rescued by bridge/controller
  Git. Scored bridge completion is switched from host `completion_gate` to a Git-service receipt +
  clean-status verifier; `not_a_git_repo` can never pass. The controller writes a sealed, read-only
  completion record `{cell_id, attempt_id, fixture_root, receipts, head, dirty, final_tree_digest,
  final_tree_digest_version}`
  as a pure projection
  of the fsynced attempt log plus controller-requested final service status, under one sealed write.
  After every bridge/control process is dead, a minimal controller-owned verifier reads only this
  authenticated record. For non-empty receipts it requires `head == last_receipt.commit`,
  `dirty == false`, and the versioned `final_tree_digest` to equal the last receipt tree's canonical
  materialization digest under the identical controller-pinned version. For empty receipts it makes
  no last-receipt comparison, requires the digest version equal the environment pin, binds the final
  filesystem digest, and records `not-delivered` regardless of whether the tree differs from the
  fixture; it never crashes or becomes importer `UNKNOWN`. Any branch mismatch fails closed. Neither
  bridge nor controller has a host-Git fallback. G2 later derives from the
  same receipts and trusted post-import graph. Status returns exactly `{head, dirty,
  final_tree_digest, final_tree_digest_version}`; the digest covers the descriptor-walked fixed
  whole fixed-worktree filesystem materialization (excluding only Git metadata) via descriptor-relative
  no-follow traversal only, rejecting
  escaping/symlinked directory components and including sorted directory structure, normalized path bytes,
  file type/mode and executable bit, sizes, regular-file bytes, and symlink target bytes, including
  all uncommitted and untracked state; timestamps, uid/gid and inode are excluded. `dirty` is true
  exactly when that whole-filesystem materialization differs from the committed HEAD tree. Missing or
  unknown digest versions fail closed. No control-plane
  or tool-plane process invokes Git against cell metadata;
- the Git service exposes only the closed shim contract above. A planted
  hook/config/fsmonitor/sshCommand/filter/helper/alias/alternate followed by every reachable shim,
  engine, bridge, cleanup, and importer action must remain inert;
- every plane runs under its own dedicated ephemeral UID, distinct from controller, other planes,
  and every other cell, with narrow Seatbelt profiles and ACLs; `0700` protects the controller run root and
  no path may relax to `0777`. Before snapshot the supervisor must prove zero processes remain for
  that UID, covering double-fork and `setsid` escape as well as its process-session ledger;
- every cell has a controller-minted unguessable bus/Redis namespace and temporary Redis ACL user
  whose server-side key pattern permits only that prefix; only the control plane receives the
  short-lived credential. Pre/post probes prove the namespace empty; cross-prefix reads/writes are
  denied. Every exit executes the single normative close below and proves neither identity nor
  namespace is reused;
- the disposable clone uses Git's `files` ref backend explicitly and preflight rejects `reftable`, so
  the active checkout is outside the scored workspace;
- pre/post hashes and status snapshots of the trusted controller clone; pre-cell Git-service status
  plus post-cell descriptor/hash accounting and importer verification for the cell (never
  controller Git on post-model cell metadata);
- pre-cell Git-service worktree accounting plus post-cell filesystem/importer accounting; cleanup
  stops all cell processes and removes the entire disposable cell root by descriptor-safe filesystem
  deletion, never `git worktree remove` on hostile metadata;
- no hidden battery key in the controller's dispatch child, bridge daemon, engine, or tool-command
  environment, proven from inside each boundary without logging the value;
- result-path and allowed-path gates over every cell;
- immediate stop if a seat writes outside its assigned worktree.

For every scored cell on every success, failure, timeout, and cancellation path, the normative close
sequence is: stop new tools; switch Git RPC status-only; drain RPC; SIGKILL and prove empty the tool
and control UIDs; disable the cell ACL user; kill its active clients; empty and delete its prefix and
user; prove retired authentication fails; obtain final Git-service status; fsync receipts; seal and
verify completion; kill and prove empty the Git-service UID; snapshot; import only if receipts are
non-empty. For non-empty receipts, score only the trusted import. For empty receipts, invoke neither
importer nor scorer: preserve G0 as the actual terminal dispatch status (completed/failed/timeout),
record G2 `not-delivered`, and emit G1/G3-G7 `NOT_SCORED` as defined in section 8; zero commits alone
never changes G0 to FAIL. Section 7 preflight must execute this same production close;
it does not define a separate ordering.

The scoring controller alone may hold `IMPLBENCH_BATTERY_KEY`. After the seat process terminates, it
launches a separate short-lived **scorer sandbox** with no network, no controller-home or evidence
mount, a read-only result tree, and bounded CPU/memory/time. Battery plaintext and the key enter only
through one-shot in-memory secret inputs and are destroyed with the sandbox. Model-generated code
never executes directly on the controller host. Prior-cell reports are never mounted back into a
later cell. Encryption does not replace filesystem or scorer confinement.
Every Python-capable runtime plane, including Git service/importer when implemented in Python, sets
`PYTHONDONTWRITEBYTECODE=1`. The six canonical sandbox roles are G1 `keyed-runner`, `broker`,
`submitted-program` and G4 `coordinator`, `suite-runner/broker`, `submitted-code`; all set
`PYTHONDONTWRITEBYTECODE=1`. Any `.pyc`/`__pycache__` present, created, loaded, or executed anywhere
in a fresh scorer/G4 sandbox filesystem, including scratch and temporary roots, fails closed and is
classified before scoring. Bytecode already present in
the imported submitted tree, or created/loaded/executed by a G1 `submitted-program` or G4
`submitted-code` UID, is model-attributable
G5 and the affected execution gate fails. Bytecode from a pinned suite mount or created/loaded by a
G4 `coordinator`, G4 `suite-runner/broker`, G1 `broker`, or G1 `keyed-runner` UID after verified clean materialization
is infrastructure `UNKNOWN`. Generated caches are never
ignored. The G1 `keyed-runner`, `broker`, and `submitted-program` each use distinct one-shot
ephemeral UIDs; G4 uses its
three roles named above. On every exit path the supervisor SIGKILLs all processes belonging to the
applicable three UIDs, reaps them, proves each UID empty, and only then returns or persists evidence.

Hidden-test confidentiality is part of scorer correctness, not merely secret-at-rest handling. The
battery decryptor/runner is exactly the G1 `keyed-runner`, not a fourth role. It and the G1
`submitted-program` are separate sandboxed processes across a narrow process/IPC boundary; the key is never present in the submitted program's process, parent process,
argv, environment, or address space. The G1 `broker` is parent of the `submitted-program`
and sibling of the `keyed-runner`; library-interface tests cross broker IPC and never import
candidate modules into the keyed runner. The submitted-program profile denies process-info operations,
debug/trace attachment, task ports, and nonessential `sysctl-read`: the submitted
program can receive only declared fixture inputs and can never read battery source/plaintext through
files, argv, environment, inherited descriptors, process inspection, loader/module state, stack
frames, tracebacks, or test-runner memory. A false-pass probe that deliberately introspects every
such channel must fail before calibration. The scorer returns only bounded gate codes and redacted
diagnostics from a static controller-owned schema containing only enums, booleans, and bounded
integers; dynamic strings and arbitrary runner
text are forbidden, with canary scanning retained only as defense in depth
before evidence release. Raw stdout/stderr, assertion bodies, tracebacks, and crash artefacts remain in a
controller-inaccessible quarantine until destroyed and never enter NDJSON, reports, prompts, or a
later cell.
For avoidance of doubt, this quarantine is a descriptor-only sink inside the existing scorer
sandbox, not a seventh scorer role and not a controller string buffer. The boundary launcher passes
write-only descriptors to the scorer roles, retains no readable descriptor or pathname grant, and
returns only the closed, bounded result schema over a separate authenticated channel. A trusted
in-sandbox encoder performs schema validation and defense-in-depth canary scanning before that result
crosses to the controller. The unlinked quarantine is destroyed only after role reaping and never
becomes an evidence artefact.
Declared fixture inputs, including temporary files, cross through a fresh bounded shared scratch
directory mounted only into the keyless broker and submitted-program sandboxes. The keyed runner
receives only opaque scratch handles over IPC, never a submitted-program-readable battery mount;
scratch is destroyed after each test and cannot carry battery source or plaintext.
Expected-result plaintext, comparison logic, and assertions never leave the keyed runner. The broker
receives only declared inputs and returns bounded candidate outputs over an authenticated one-way
response channel; submitted code cannot observe expected values or runner messages.

The controller also treats the disposable cell clone's entire Git directory as hostile. A separate
short-lived **Git importer sandbox** copies only validated loose-object/pack bytes and candidate ref
OIDs as inert data into a fresh importer-owned Git directory; candidate OIDs are accepted only when
they exactly match the ordered, fsynced Git-service receipts in the controller's out-of-cell attempt
log. Cell-stored refs are non-authoritative. The importer requires the first receipt parent to equal
the fixture OID, each later receipt's sole ordered parent to equal the prior receipt commit, every
parsed commit's ordered parents and tree to match its receipt, controller-pinned SHA-256 over a
versioned canonical tree serialization to reproduce the materialized-tree digest, and imported
`HEAD` to equal the final receipt commit. For every parent-to-commit edge the importer independently
walks both parsed trees, computes the exact changed-path set, requires equality with the receipt's
observed paths, and checks that computed set against the controller-owned allowlist; receipt paths
alone are never authoritative. It never invokes Git with the cell
repository as cwd or `GIT_DIR`. System, global, and local config are absent in the fresh directory;
hooks are disabled; external helpers, replacements, alternates, filters, attributes, and non-local
protocols are denied. The importer verifies a bounded linear commit chain rooted at the exact
fixture OID and ending at the last receipted OID, preserving every intermediate commit required for
G4; it verifies changed paths and complete object closure, runs bounded `git fsck --strict` inside
the importer sandbox against only the fresh importer-owned Git directory, then emits a
content-addressed bundle.
Importer internal output uses fixed-width validated OID/digest byte fields plus enums, booleans, and
bounded integers; its public evidence schema omits byte fields and permits only the latter static
types. Filenames, Git text, parser errors, and other dynamic strings remain quarantined and never
enter evidence or later prompts.
The controller imports only that
bundle into its own clean clone; it never runs Git against the untrusted cell repository or copies
its config, hooks, alternates, worktree metadata, or non-object files.
Every controller-clone Git invocation uses a scrubbed environment with system/global config disabled,
`core.hooksPath=/dev/null`, fixed protocol/file settings, and no repository-controlled helpers,
filters, replacements, or environment overrides.
The importer performs one iterative descriptor-held snapshot pass with a bounded explicit queue
(never recursive call-stack traversal). It incrementally enforces file-count, declared/actual bytes,
depth, open-descriptor, memory, disk and wall-time limits before each allocation/write; it copies
each accepted source descriptor into an importer-owned bounded spool before closing or advancing, and
never reopens a model-controlled pathname. Overflow deletes the spool and fails closed. The pass
walks without following links and accepts only regular files with link
count one only at canonical paths matching loose objects
`objects/[0-9a-f]{2}/[0-9a-f]{38}`, packs `objects/pack/pack-[0-9a-f]{40}.{pack,idx}` and the single
controller-named candidate ref; every other Git-directory filename is rejected. It rejects symlinks, hardlinks, devices, sockets,
FIFOs and path traversal, and rechecks type, inode, size, and content digest before and after each
bounded copy. Every loose object and reconstructed packed object must cryptographically hash to its
declared OID after copy; mismatch is fatal. The immutable snapshot begins only after the controller
has proved the entire cell process session empty. The
importer enforces fail-closed CPU, memory, wall-time, disk-byte, object-count, pack-size,
decompression-ratio, tree-depth and commit-count limits before and during object parsing. Imported
trees reject gitlinks and absolute or root-escaping symlink targets; scorer materialization never
follows links outside its root and opens result inputs with no-follow semantics.

## 5. Balanced execution order

Run one scored cell at a time. Parallel execution is faster but introduces host CPU, provider burst,
and rate-limit confounds. Worktree isolation prevents file collisions; it does not remove resource
contention.

Within each pair and task, alternate first position:

```text
if (repetition + task_index) is even: Pi → Open Interpreter
if (repetition + task_index) is odd:  Open Interpreter → Pi
```

Alternate which model pair runs first by repetition:

```text
repetitions 1 and 3: GLM pair → Kimi pair
repetitions 2 and 4: Kimi pair → GLM pair
```

Task order is generated once from a recorded random seed, then reversed on even repetitions. The
manifest records the seed and full scheduled order before the first scored dispatch. The schedule
must not be changed after observing results.

## 6. Existing Implementor Bench: required readiness work

The existing bench is the scoring core, but the current tree is not ready for this live matrix.
These are blockers, not optional cleanup:

**Review boundary:** design freeze approves this contract; it does not assert that the readiness
implementation already exists. Absence of a listed readiness mechanism in the current source tree
confirms that its numbered blocker is still open; it is not, by itself, a defect in this design.
Conversely, implementation may not weaken, defer, or silently reinterpret a blocker merely because
the design panel froze. `execution_base_sha` remains unset until every blocker below is implemented,
independently verified, and merged. No calibration, pilot, or scored cell may run before that later
readiness gate.

The r4 findings map to already-open blockers as follows: Open Interpreter routing → item 1;
host-executed Git against a cell repository → item 12; host-executed hidden batteries → item 11;
unchecked model/reasoning provenance → item 4; undifferentiated G0/G1 infrastructure attribution →
item 8; static-only seat preflight → item 13. Those findings are implementation evidence that the
gate is correctly closed, not evidence that the design permits the unsafe behavior.

1. **Open Interpreter engine:** add `openinterpreter` to ARB with tool prefix `interpreter`; use
   `interpreter app-server --listen stdio://`; require provider and harness explicitly; pin Open
   Interpreter `0.0.21`; extract the error from failed `turn/completed` payloads. Every harness
   shell/file/code-execution path must be intercepted into the Seatbelt tool plane; any in-process
   or unprofiled tool path makes that arm non-scoreable.
   Freeze and verify the complete engine/harness/adapter/provider-client binary and content-digest
   manifest described in section 2, not only Open Interpreter's package version.
2. **Runtime dependency:** `scripts/implbench` currently fails under the active interpreter because
   PyYAML is not installed there. Give the bench a declared runnable environment and prove
   `scripts/implbench --help` from a fresh sync.
3. **Concurrency contract:** `--concurrency` is parsed but currently unused. Implement the pinned
   contract or hard-refuse unsupported values. This bake-off sets it to `1` deliberately.
4. **Authoritative provenance:** replace unchecked `IMPLBENCH_MODEL_<SEAT>` declarations with a
   frozen seat manifest reconciled against the daemon's effective provider/model/harness config.
   Persist `model_declared`, `model_verified_via`, engine version, harness version, corpus version,
   config digest, capability manifest, `reasoning_requested`, `reasoning_effective`, and
   `reasoning_verified_via` in every NDJSON record. Refuse a pair whose effective reasoning levels
   are unknown or unequal. Only a provider/harness runtime acknowledgement may populate
   `reasoning_effective`; request/config echo is forbidden.
   Apply the identical independent runtime-acknowledgement rule to every exposed generation/runtime
   control from section 2 and persist its requested/effective/verified-via triple. Preflight and each
   scored cell reject echo, `UNKNOWN`, drift, or material pair mismatch.
5. **Explicit cell and attempt identity:** require `cell_id`, `attempt_id`, pair, arm, repetition,
   fixture SHA, and schedule index in every authoritative record. Include `cell_id` and `attempt_id`
   in run/result refs and worktree names; every attempt must be recoverable without timestamps.
6. **Matrix driver:** implement the frozen schedule in section 5, run exactly one cell at a time,
   verify the expected fixture/config digests before dispatch, and refuse schedule mutation after
   `manifest.json` is written.
   Before calibration, atomically write the controller-owned environment pin manifest containing
   every section 2 control, OS/runtime/binary/profile/allowlist digest, each task's public-suite
   artefact OID/path, digest, and digest version, the controller-pinned
   `final_tree_digest_version`, the encrypted G1 battery artefact path/version/content digest and
   opaque key-version identifier (never a key or reusable key-derived digest), and the complete analysis
   rule. Any later difference starts a new run ID; no field may be selected after calibration output.
7. **Observed telemetry:** persist start/end timestamps, wall time, terminal status, retry count,
   tool-call count, and provider/engine errors. Token and cost fields may be `UNKNOWN`; they must not
   be invented.
8. **Infrastructure classification:** distinguish model/turn failures from bus, daemon, provider,
   sandbox, worktree, and scoring failures. A provider/bridge wait with no model execution, daemon
   death, sandbox launch failure, or scorer launch failure is infrastructure: G0 and all dependent
   gates emit `UNKNOWN`, not `FAIL`, and retain the concrete cause. A model-issued tool command that
   exceeds its declared resource limit is a model G0 failure. A healthy scorer whose execution of
   model-authored code exceeds its declared limit is a model G1 failure. If G0 is infrastructure
   `UNKNOWN`, G1-G7 for that attempt are also authoritative `UNKNOWN` with the same root cause; raw
   observable diagnostics may be retained separately but are not scored.
9. **Per-cell runtime isolation:** provision and verify the filesystem boundary, fresh state home,
   engine-control/tool/Git-service split, engine retirement, battery-key absence at every seat-side
   process boundary, provider-credential absence from model-invoked tools, no in-process/unprofiled
   Open Interpreter tools, and cleanup rules in section 4 for both engines.
10. **Capability contract:** record both effective tool surfaces and execute positive and negative
    capability probes. Refuse the pair if their capability classes cannot be matched.
11. **Scorer sandbox:** run generated code and hidden batteries only in the isolated scorer described
    in section 4.4 against the post-import pinned commit; prove network, host-filesystem,
    secret-retention, hidden-test introspection/exfiltration, redacted-output, and resource-limit
    denial.
12. **Quarantined Git service/import:** implement the closed Git-service shim and importer in section
    4.4 and adversarially prove that raw Git, arbitrary RPC, cell-controlled config, hooks,
    fsmonitor, alternates, helpers, filters, paths, links, and malformed object graphs cannot execute
    on or escape into any credentialed/controller plane.
13. **Executable seat preflight:** extend `scripts/seat-preflight --strict`. It must consume a frozen
    redacted seat manifest, instantiate and destroy one representative preflight-only cell-suffixed
    daemon per arm through the exact scored config/workdir/home/control-plane/tool-plane path, run
    section 7's seat-scoped criteria 3-11, emit a value-free JSON report, and exit nonzero on any
    mismatch or cleanup residue. Its mandatory bus sub-gate creates a temporary ACL user/prefix,
    proves pre-empty and cross-prefix read/write denial, then invokes the normative production close
    and verifies the retired user/prefix are unusable afterward.
14. **Aggregate readiness and runner truth:** add a controller-owned bakeoff preflight that invokes
    strict seat preflight, hostile importer and scorer probes, canonical-path/accounting checks, then
    `scripts/implbench validate` and a full known-good Codex calibration run. It emits one redacted
    manifest of sub-gate results and fails closed unless the known-good seat clears every cluster.
    It independently executes the section 4.4 normative production close after its bus probes rather
    than trusting the seat report; it defines no second ACL lifecycle.

No scored Open Interpreter/Pi comparison starts until all fourteen are evidenced green.

## 7. Seat pre-flight

Before the first scored cell:

1. Rotate the credentials previously pasted into chat; put replacements only in controller-owned,
   mode-0600 seat env files or an external command-backed credential source. Freeze the non-secret
   config before the run, record its digest, and make it immutable for the run. The engine control
   plane may read its frozen config through a secret mount or inherited descriptor; the tool plane
   must not be able to read it. Do not load a live env file from the active checkout.
2. Run `scripts/seat-preflight --strict` on each arm's frozen definition. The verifier must create
   and destroy a representative cell-suffixed daemon using the exact scored runtime path, not inspect
   static configuration only.
3. For each representative preflight daemon created by step 2, ping its preflight-only
   cell-suffixed agent ID and verify its registry path equals that representative disposable clone,
   never the controller clone or active checkout. Scheduled scored-cell IDs do not exist yet and
   are not pinged here.
4. Run one exact-text, no-tool turn per seat.
5. Run one read/edit/test/commit smoke fixture per seat through `agent-dispatch --worktree`.
6. Confirm the reported model/provider/harness from configuration and engine version, never from the
   model's prose.
7. During the exact-text smoke, require each provider/harness to emit an independent runtime
   acknowledgement of effective reasoning and every exposed generation/runtime control; persist all
   requested/effective/verified-via triples and fail readiness on `UNKNOWN`, material inequality,
   unsupported asymmetry, config/request echo, or model prose.
8. Confirm the base checkout stays byte-clean and the engine process is retired after the turn.
9. Confirm `IMPLBENCH_BATTERY_KEY` is present only in the scoring controller and absent from the
   dispatch child, bridge daemon, engine, and a shell command executed inside each seat boundary.
10. Confirm the per-cell home is initially empty, the cell cannot read the base checkout, sibling
   worktrees, controller evidence, or arbitrary network from a tool command, and both the home and
   runtime are destroyed afterward.
11. From a model-invoked shell in the tool plane, confirm provider credential names and values are
    absent and every network destination is denied, explicitly including `127.0.0.1`, `::1`, the
    bridge/Redis endpoints, and external egress. Separately, run a control-plane probe that confirms
    only the configured provider endpoint(s) and seat bus/Redis endpoint(s) are reachable; all other
    destinations are denied. Record endpoint classes and boolean results, never secret values.
12. After strict seat preflight returns, the controller-owned aggregate preflight proves shim
    `status/add/commit` succeeds at the canonical tool-plane paths while every raw Git path is
    denied, then runs a hostile Git-import
    fixture that writes hooks/config/alternates and confirms none execute or enter the controller
    clone.
13. Prove the control-plane profile denies the canonical cell Git directory, `.git` pointers, every
    host/bundled Git executable, and Git config/environment bypass. Then execute the single normative
    production close algorithm in section 4.4 without restating or reordering it. Preflight must
    exercise both section 4.4 branches and validate the authoritative output records and postconditions
    produced by that algorithm; it must never recompute, mock, restate, or reinterpret G0/G2 or
    importer/scorer behavior here.
    Bridge prose is never authoritative.
    In both seat and aggregate preflight, prove the bus namespace is empty before dispatch,
    cross-prefix reads/writes are denied, the namespace is empty after close, and destruction
    succeeds; no readiness gate may infer these properties from endpoint reachability alone.
14. The controller-owned aggregate preflight runs a hostile scorer probe attempting host-file,
    controller-secret, network, fork-bomb, post-exit persistence, hidden-test introspection, and
    output exfiltration; all must be denied without weakening the real hidden battery.

Any failed pre-flight is infrastructure evidence, not a model loss.

## 8. Gates and measurements

The authoritative result remains append-only NDJSON, one record per task and gate.

### Primary correctness evidence

- **G1 hidden battery:** real behavioral correctness unknown to the implementor.
- **G3 allowed paths:** scope discipline.
- **G5 prohibitions:** forbidden implementation mechanisms.
- **G6 baseline preservation:** regression safety.
- **G7 fabricated completeness / phantom SHA:** evidence honesty.

### Delivery and process evidence

- **G0 dispatch completion:** completed versus failed/timeout.
- **G2 delivery:** agent-delivered or not-delivered. Orchestrator rescue/auto-commit is disabled for
  scored seats because it would require credentialed control-plane Git on hostile cell metadata.
- **G4 TDD demonstration:** only after successful import, the common scorer-sandbox launcher used by
  G1 starts a G4-specific, entirely keyless topology: a one-shot coordinator UID that never imports
  candidate modules, a one-shot suite-runner/broker UID that loads only the pinned public suite and
  sends declared calls over narrow IPC, and a one-shot submitted-code UID that alone imports or
  executes candidate modules. No battery key,
  G1 `keyed-runner`, battery mount, or expected-result material enters any G4 process. It
  re-materializes each receipted commit OID read-only with no network/host/evidence mounts and bounded resources.
  On every success, failure, timeout, and cancellation path, the supervisor SIGKILLs exactly all
  coordinator, suite-runner/broker, and submitted-code UID processes, reaps them, proves every UID
  empty, and only then persists
  or returns a G4 receipt. It re-executes only the task's pre-pinned public suite. Every receipt must
  contain the exact frozen suite artefact OID, digest, and digest version for that task. It returns
  sequenced receipts `{cell_id, attempt_id, commit_oid, public_suite_oid, public_suite_digest,
  public_suite_digest_version, outcome_enum, sequence}`
  through a nonce-authenticated channel and fsyncs them to the attempt log. Two immutable
  controller-owned authenticated records close the scoring inputs and later G4 outputs without
  inventing another OS process or UID. First, after import and before any G1 or G4 scorer launch, a
  **pre-scorer input attestation** binds the environment-pin-manifest, completion-record, and
  imported-graph digests. The controller writes, fsyncs, re-reads, and verifies that closed record;
  scorers receive only its verified digest-bound projection. An in-memory value, a record written
  after scorer launch, or release of the full record does not satisfy this boundary. Second, only
  after the G4 topology has returned its sequenced receipts and every G4 process has been reaped, a
  **post-G4 receipt attestation** binds the verified pre-scorer-attestation digest and ordered
  G4-receipt-list digest. The controller writes, fsyncs, re-reads, and verifies this second record
  before G4 classification or report release. A pre-scorer record cannot claim a G4 receipt digest,
  and no G4 verdict is authoritative without the post-G4 record. The controller rejects replay,
  identity/pin/version mismatch, receipt reordering, or digest mismatch at either boundary.
  Internal authenticated records use fixed-width validated OID/digest byte fields plus enums and
  bounded integers. Authoritative public evidence may additionally expose only validated non-secret
  fixed-width OID/digest/version fields needed to independently verify pins and attestations; it
  otherwise exposes only enums/booleans/bounded integers. Neither permits
  free-form strings; raw stdout/stderr is quarantined. G4
  requires FAIL at an earlier receipted commit and PASS at a later one for the same pinned suite;
  commit OIDs alone never prove red/green. Report separately, never fold into correctness.
  Fewer than two receipts, or no earlier-FAIL/later-PASS pair, is `NOT_DEMONSTRATED`/`NOT_SCORED`,
  never PASS and never a model correctness failure.
- wall time, tool-call count, engine/provider failures, and retries.

`UNKNOWN` is never converted to `FAIL`. It identifies an instrumentation or infrastructure gap that
must be repaired and rerun. G0 `UNKNOWN` cascades to authoritative `UNKNOWN` for G1-G7 on that
attempt. A rerun keeps the original record, receives a new cell-attempt ID, and states the repaired
cause. Resource exhaustion attributable to a model-issued tool command is G0 `FAIL`; resource
exhaustion attributable to model-authored code inside an otherwise healthy scorer is G1 `FAIL`.
If receipts are empty or G2 is `not-delivered`, no fixture-tip surrogate is scored: G1, G3, and
G5-G7 are `NOT_SCORED` while G2 alone records non-delivery; G4 is also `NOT_SCORED`, never `FAIL`.
The importer is not invoked for that attempt, so this path cannot become importer `UNKNOWN`.
If a receipt-consistent sealed attempt later fails importer integrity, fsck, or materialization, G0
and all dependent gates are append-only infrastructure `UNKNOWN`, the run stops for repair, and the
failure is never charged as a model correctness or delivery loss.

Every authoritative attempt record also carries the closed `failure_category` enum
`none | model-implementation | protocol-import-infrastructure | other-infrastructure`. Importer
parsing/integrity/attestation failures, malformed or truncated boundary frames, protocol-version or
identity mismatches, and receipt/import IPC failures are
`protocol-import-infrastructure`; they force the existing authoritative `UNKNOWN` cascade and never
count as an implementation loss. Model-authored code or a model-issued operation failing inside a
healthy, correctly evidenced boundary is `model-implementation`. Provider, bus, daemon, sandbox
launch, host, ACL, and scorer-supervisor faults are `other-infrastructure`. The public enum is
value-free; the concrete private cause remains append-only quarantined evidence. Ambiguous
attribution is `other-infrastructure`, never guessed into `model-implementation`.

## 9. Analysis and interpretation

Analyse GLM and Kimi separately. For every pair report:

- the per-task, per-repetition G0-G7 grid for both arms;
- G1 paired wins/losses/ties by task and an exact paired sign test for every nonzero non-tied sample;
  label results with fewer than eight non-tied pairs `underpowered` rather than omitting the test;
- every G3/G5/G6/G7 regression as a named finding, not an averaged penalty;
- delivery and TDD shapes separately;
- median and p95 wall time for successful cells only, with failure counts beside them;
- repeated task-family asymmetries and within-arm variance.

The frozen primary unit is the matched `(model family, task, repetition)` pair: one Pi arm and one
Open Interpreter arm with identical fixture/config pins. G1 `PASS` beats G1 `FAIL`; equal G1 values
tie. A pair is non-scoreable when either arm is `UNKNOWN`, `NOT_SCORED`, missing, or pin-mismatched;
it is excluded without imputation and reported by arm and `failure_category`. Attempt number zero is
the scheduled attempt. A later attempt is eligible only through a contiguous controller-authenticated
repair chain: after an attempt ends with authoritative G0 `UNKNOWN` and `failure_category` of
`protocol-import-infrastructure` or `other-infrastructure`, and before another dispatch, the
controller appends and fsyncs a repair-authorization record binding the prior attempt ID/number, next
attempt ID/number (`prior + 1`), closed failure category, quarantined cause digest, and controller
sequence. No missing link, post-dispatch authorization, model-implementation failure, or
model-attributable gate `FAIL` can authorize replacement. Select attempt zero when it is fully
authoritative and scoreable; otherwise walk only that contiguous authorization chain and select the
lowest attempt number that is fully authoritative and scoreable. All attempts and authorizations
remain visible append-only. If the chain ends or is invalid before such an attempt, the cell and its
matched pair remain non-scoreable.

Repetitions are not averaged. Report per-task counts of Open Interpreter wins, Pi wins, ties, and
non-scoreable pairs across the four repetitions; call a task-family direction repeatable only when
one arm wins at least three repetitions with zero wins for the other arm. Across all scoreable
non-tied matched pairs, report the two-sided exact sign-test p-value and an exact 95% binomial
confidence interval for the Open Interpreter win probability. For `w` Open Interpreter wins, `l`
Pi wins, and `n = w + l`, the two-sided exact sign-test p-value is
`min(1, 2 * BinomCDF(min(w,l); n, 0.5))`. The interval is the central equal-tail 95% exact
Clopper-Pearson interval: lower bound `0` when `w=0`, otherwise
`BetaInverse(0.025; w, n-w+1)`; upper bound `1` when `w=n`, otherwise
`BetaInverse(0.975; w+1, n-w)`. At `n=0`, report p-value `1`, interval `[0,1]`, and
`underpowered`. Fewer than eight non-tied pairs is `underpowered` and cannot declare a winner.

The hard-floor domain is every deterministically selected authoritative attempt for that model pair,
including G1 ties and attempts outside the non-tied sign-test sample. `hard_floor_clear(arm)` is true
exactly when zero such attempts for that arm have `FAIL` on G3, G5, G6, or G7; a failure shared by
both arms therefore clears neither arm. With at least eight non-tied pairs, declare an Open
Interpreter (or Pi) win only when its win count is larger, the two-sided p-value is at most `0.05`,
and `hard_floor_clear` is true for that arm. Otherwise the predeclared primary result is `tie`; any
repeatable task-family split is still reported as `mixed/decorrelated`. This rule is manifest-pinned
before calibration and cannot be selected after observing results.

Do not emit a ranked leaderboard or composite score. Classify each model pair into one of these
evidence shapes:

1. **Open Interpreter dominated:** repeatable correctness or safety regressions with no offsetting
   task-family capability.
2. **Operationally equivalent:** no repeatable gate difference; choose on maintenance/latency, but
   describe the result as equivalence within this corpus, not universal equivalence.
3. **Open Interpreter adds capability:** repeatable hidden-battery passes on task families Pi misses,
   with `hard_floor_clear(Open Interpreter)` under the frozen primary domain.
4. **Mixed/decorrelated:** each arm owns repeatable task-family wins; retain both and route by task
   class rather than declaring one universal winner.

No production promotion follows automatically. The final artefact makes a recommendation and names
the evidence; the user decides routing.

## 10. Stop rules

Stop the affected model pair immediately on any of:

- wrong model, provider, harness, or engine version;
- context/session reuse across cells;
- hidden battery key visible to a seat;
- fixture SHA mismatch between paired arms;
- write outside the assigned worktree;
- base-checkout or sibling-worktree drift;
- malformed/missing NDJSON evidence;
- failed terminal status whose concrete provider error is discarded;
- unknown or unequal effective reasoning, or an effective value supported only by request/config
  echo rather than a provider/harness runtime acknowledgement;
- three infrastructure failures with the same cause.

Provider 401/429/5xx, bridge busy, provider/bridge wait timeout, sandbox launch denial, worktree setup
failure, scorer launch failure, and daemon death are `UNKNOWN` until diagnosed. A model tool-resource
timeout or a healthy scorer timing out model-authored code is `FAIL`, not infrastructure `UNKNOWN`.
Do not quietly retry either class and count the successful retry as the original cell.

## 11. Evidence package

One controller-owned directory, excluded from every seat's OS-level readable mounts, contains:

```text
manifest.json                 # base SHA, corpus SHA, versions, random seed, full order
cells.ndjson                  # cell/attempt schedule and terminal outcome
results/<run-id>.ndjson       # authoritative G0-G7 records
reports/<run-id>.json         # derived per-seat report
seat-config-redacted/         # configs with secret values removed
preflight/                    # pings, exact-text and commit smoke evidence
git-refs.txt                  # fixture/result refs and SHAs
worktree-accounting.txt       # pre/post worktree-list proof
final-comparison.md           # two pairwise conclusions; no composite leaderboard
```

The manifest stores secret **names**, never values. Every NDJSON row carries the immutable cell and
attempt identities, fixture/config digests, and requested/effective reasoning provenance, so retries
cannot overwrite or masquerade as their originals. Preserve `refs/implbench/runs/*` and
`refs/implbench/results/*` until the decision is accepted; pruning is a separate explicit action.
Scorer output in this package is limited to bounded gate codes and redacted diagnostics; raw
stdout/stderr, tracebacks, assertion bodies, battery-derived text, and crash artefacts are forbidden.

## 12. Execution phases

1. **Readiness implementation:** Open Interpreter ARB engine plus the narrow Implementor Bench fixes
   in section 6, developed and reviewed in their own feature/task worktrees.
2. **Calibration:** hermetic bench suite, adversarial validation, known-good Codex run, then one
   unscored task through all four new seats.
3. **Pilot:** execute scored repetition 1 but quarantine it provisionally; inspect instrumentation
   and isolation without comparing models. If its evidence package validates unchanged, freeze a
   content hash over the complete repetition-1 manifest, configs, refs, and append-only rows and
   include those same cells in the final 128-cell analysis. If validation fails, retain every invalid
   attempt append-only. Rerun with new attempt IDs only for classified G0 infrastructure `UNKNOWN`;
   any model-attributable correctness or stop-rule failure remains part of repetition 1 and may stop
   the pair rather than being replaced.
4. **Full matrix:** execute scored repetitions 2-4 only after repetition 1 validates; together the
   validated pilot cells and repetitions 2-4 are the declared four repetitions / 128 dispatches.
5. **Analysis:** freeze NDJSON, render both pairwise reports, investigate every `UNKNOWN`, and produce
   the final comparison.
6. **Human decision:** promote, retain as adjunct, route by task family, or reject each Open
   Interpreter seat independently.

The prerequisite implementation work should use Workflow B because it changes engine protocol,
auth/config handling, and evaluation evidence. Per the pipeline manual, the user must explicitly
confirm Workflow A or B before the first implementation dispatch. Scored benchmark turns themselves
are experiment executions, not implementation-review workflow stages.

## 13. Explicitly out of scope

- MiniMax and OpenCode Go seats or credentials.
- Reviewer-role ranking.
- Cross-model GLM-vs-Kimi conclusions.
- Automatic routing or trust changes.
- Production daemon installation.
- Pruning result refs or deleting the evidence package.

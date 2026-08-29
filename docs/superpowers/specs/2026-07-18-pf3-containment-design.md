# PF3 full-stack containment design — FABA SDK harness (v4, panel-folded)

**Status:** v4 design with owner decisions and decorrelated-panel findings folded
**Scope:** the SDK-harness form in `tools/faba/faba_launch.py`; design only
**Decision sought:** approve the split controller/worker architecture and the recommended
dedicated-per-round-UID macOS launch boundary

## Problem

FABA is a per-round bounded suborchestrator. The parent shells
`tools/faba/faba_launch.py`; that harness starts one root Claude Agent SDK session,
validates the session's `decision-record.md` from disk, publishes the validated record
to ARB Memory, gates on its own receipt, and exits. The process is the round, so process
death is the automatable `/clear`. These lifecycle and success semantics are stated in
the module itself (`tools/faba/faba_launch.py:3-15`), and the record rather than the
agent's final prose is the authority (`tools/faba/faba_launch.py:6-9`,
`tools/faba/faba_launch.py:196-204`).

The current integrity design is sound only if the SDK session cannot exceed the
declared tool ceiling, cannot obtain the Memory-bus credential, and cannot leave a
credentialed process alive across publication. None of those conditions is fully
enforced today. PF3 must close all three as one boundary, because fixing any one in
isolation leaves another route to forge the receipt on which success depends.

This design recommends separating the current process into:

1. a **privileged controller/publisher**, which alone reads the Memory credential,
   validates the record, and executes `DEL -> publish -> poll`; and
2. an **exec-isolated round worker**, which owns the SDK session, receives only
   non-secret inputs, runs under a deny-by-default tool gate plus an OS filesystem
   boundary, and is terminated and reaped before publication begins.

The controller must fail closed if any containment startup proof or teardown proof
fails. A failed proof means no publish and a non-zero round exit; it is not a warning.

Receipt keys are predictable to the worker, so credential absence alone is not the
Hole-3 forgery control. The primary control is a deny-default outer OS network boundary
for the whole worker tree that allow-lists only the destination-pinned loopback provider
broker: a survivor with no route to Redis cannot LPUSH a forged receipt, whether or not
that bus is authenticated. This assumes a TCP bus. For a Unix-socket bus, the socket
path is omitted from the worker's readable filesystem and explicitly denied. Bus
authentication is opportunistic additional depth when present, not a PF3 launch
precondition.

## Current state: the three containment holes

### Hole 1 — the declared ceiling is an additive approval surface

`run_session` currently passes the manifest's non-empty `allowed_tools`, its
`disallowed_tools`, and `permission_mode` directly into `ClaudeAgentOptions`
(`tools/faba/faba_launch.py:252-259`). The shipped manifest allows Read, Grep, Glob,
Write, Edit, Bash and three local Memory read tools
(`tools/faba/manifest.json:4-13`), while selecting `acceptEdits`
(`tools/faba/manifest.json:24`). No `can_use_tool` callback is supplied at this call
site (`tools/faba/faba_launch.py:252-259`). Thus the manifest describes intent but is
not a deny-by-default mediation point; an account/user pre-approval can take an
additive auto-approval path without consulting FABA.

The bridge's Agent SDK engine already encodes the relevant SDK invariant. Its
normative helper returns `permission_mode="default"`, `allowed_tools=[]`, and
`setting_sources=[]`, because `can_use_tool` fires only on the ask path and either an
allow-list or loaded settings can bypass that path
(`src/agent_redis_bridge/engines/agent_sdk_mediation.py:49-52`). The engine then wires
the callback through `can_use_tool=self._gate`
(`src/agent_redis_bridge/engines/agent_sdk.py:275-281`), fails closed on callback
exceptions (`src/agent_redis_bridge/engines/agent_sdk.py:347-352`), and refuses to
serve unless the connected options retain that fail-closed baseline
(`src/agent_redis_bridge/engines/agent_sdk.py:384-405`). FABA currently uses none of
that pattern.

### Hole 2 — the credential is absent from the child environment but readable on disk

The positive current property is real: `parse_env_file` returns selected values rather
than exporting them (`tools/faba/faba_launch.py:76-92`), and `main` removes an explicit
`ARB_MEMORY_REDIS_URL` from `os.environ` and retains the chosen value in a local
variable (`tools/faba/faba_launch.py:310-319`). However, the same command line tells
the harness which `--env-file` to read (`tools/faba/faba_launch.py:302`), and the SDK
session is launched with Bash and repo read access. The repository documentation names
the resulting attack exactly: the child can read the env file itself, and the clean
slate receipt deletion only narrows the forgery window
(`tools/faba/README.md:48-60`). Environment non-inheritance is therefore not
credential unreachability.

`cwd` does not enforce a filesystem boundary. It only points the SDK at the workspace
(`tools/faba/faba_launch.py:252-254`). Likewise, a Read-tool denial is not sufficient
against `Bash cat`; the SDK's installed `SandboxSettings` docstring explicitly says
filesystem restrictions are permission rules, while its sandbox controls Bash, and a
shell is a second access path
(`/Users/<user>/<workspace>/.venv/lib/python3.14/site-packages/claude_agent_sdk/types.py:873-916`).
PF3 needs a kernel-enforced filesystem view or denial, not another prompt rail.

### Hole 3 — a descendant can survive into receipt gating

After validation, `publish_and_gate` opens the Redis client, deletes the deterministic
receipt key, publishes, then polls that key
(`tools/faba/faba_launch.py:196-227`). The code already records the residual precisely:
session end is not process-group death, so a detached credential-harvesting descendant
can re-seed the key after deletion (`tools/faba/faba_launch.py:206-212`). In `main`,
publication follows `asyncio.run(run_session(...))` directly; there is no intervening
process teardown or survivor proof (`tools/faba/faba_launch.py:375-391`).

The SDK itself does not supply the missing boundary. In the installed 0.2.107 package,
the transport starts the Claude CLI with `anyio.open_process` but without a new
session/process-group argument
(`/Users/<user>/<workspace>/.venv/lib/python3.14/site-packages/claude_agent_sdk/_internal/transport/subprocess_cli.py:474-483`),
and its exit cleanup signals tracked direct children, not a whole descendant tree
(`/Users/<user>/<workspace>/.venv/lib/python3.14/site-packages/claude_agent_sdk/_internal/transport/subprocess_cli.py:40-47`).
Both facts were checked on 2026-07-18; grep found no `start_new_session` or `setsid` in
that transport.

## SDK reality checked for v4

The actual project venv reports SDK **0.2.107**, not the ADR's assumed 0.2.117:

```sh
/Users/<user>/<workspace>/.venv/bin/python -c \
  'import claude_agent_sdk; print(claude_agent_sdk.__version__)'
# 0.2.107
```

Inspection of `ClaudeAgentOptions.__dataclass_fields__` in that interpreter confirmed
the fields needed here: `allowed_tools`, `disallowed_tools`, `can_use_tool`,
`permission_mode`, `setting_sources`, `strict_mcp_config`, `env`, `add_dirs`, `user`,
`hooks`, `agents`, `permission_prompt_tool_name`, `sandbox`, and `mcp_servers`. The
version discrepancy does not contradict the ADR's substantive conclusion.
`can_use_tool` is insufficient
**alone**; the working bridge pattern makes it sufficient for ask-path tool mediation by
pairing it with empty `allowed_tools`, empty `setting_sources`, and
`permission_mode="default"`. Auto-approved read tools do not reach the callback.
Nothing observed in 0.2.107 justifies a different FABA pattern. A live 0.2.107
additive-preapproval probe remains a prerequisite before implementation is certified,
and the same probe must rerun on every SDK/CLI upgrade.

## Approaches considered

### A. Tool callback only

Port `gated_option_kwargs`, add a FABA callback, and validate Write/Edit paths and Bash
command text in Python. This is the smallest Hole 1 change and should be part of every
approach. It cannot close Hole 2: shell syntax, interpreters, symlinks, encodings, and
new executables make a Bash command-text deny-list non-exhaustive. It also cannot reap
descendants. **Rejected as a complete PF3 solution.**

### B. One process with the SDK Bash sandbox

Add `SandboxSettings(enabled=True, allowUnsandboxedCommands=False)`, permission rules,
and keep the current in-process publish flow. This improves Bash confinement, but it
mixes the secret-bearing publisher with the untrusted SDK runtime in one address space,
does not by itself place built-in Edit/Read behind an outer OS boundary, and provides no
controller that can kill the SDK's whole process group before publishing. The SDK
docstring also says filesystem read/write policy comes from Read/Edit permission rules;
`sandbox.enabled` alone is not a filesystem allow-list. **Rejected as the primary
boundary; retained as defence in depth inside the worker.**

### C. Privileged controller plus OS-contained worker (recommended)

The controller materialises the real round inputs and prompt, reads the credential, and
then `exec`-launches a secret-free worker in its own lifecycle boundary. The worker uses
the bridge's callback-gate pattern. On macOS the recommended mechanism is a dedicated
per-round UID plus an outer Seatbelt profile denying credential reads and all writes
except the round workspace and private runtime scratch, plus the SDK Bash sandbox. On
managed Linux the target mechanism is a
mount-minimal container with a read-only repo view that omits secret files, a read-write
workspace, and a cgroup/container kill at teardown.

This approach is recommended because it makes the trust boundary match the dataflow:
the process that can run agent-chosen commands never receives the Memory secret, and the
process that holds the secret never runs the SDK. It also gives the controller a point at
which it can establish the teardown-before-publication ordering.

The macOS profile is a denial boundary rather than absence by construction, so it needs
a per-run deny canary; the readable source view must omit secrets by construction, with
a duplicate-secret scan retained as advisory defence in depth. The Linux container is
the stronger end state because the credential file is absent from the worker namespace.

## Design

### Boundary and lifecycle

The outer `faba_launch.py` command remains the stable user interface. Internally it has
two roles; the implementation may use a private `--session-worker` mode or a small new
worker module, but the privileged and unprivileged code paths must be separate exec
images, not a `fork` that retains the controller's Python memory.

The sequence is normative:

1. Before SDK or publish work, fail closed with a distinct exit if the controller's
   **initial** process environment contains `ARB_MEMORY_REDIS_URL` or any bus/provider
   secret. This makes the env file the only credential ingress and prevents macOS
   launch-time environment introspection from recovering a value later removed from Python.
2. Parse and validate non-secret arguments. Resolve every path with `realpath`, reject
   symlinks for the env file, and mint `run_id`, `request_id`, and record id in the
   controller.
3. Materialise `round-input.json`, any prior record, and the fully composed production
   prompt through production helpers. The existing SDK path writes these bytes before
   launch (`tools/faba/faba_launch.py:325-365`); implementation should extract that code
   into a callable `materialise_workspace`/`build_brief` surface rather than duplicate it.
4. Read the Memory credential into the controller only. Never include the env-file path,
   Redis URL, request receipt key, or secret-bearing configuration path in worker argv,
   environment, prompt, cwd, or IPC.
5. Construct and prove the OS boundary. If the host mechanism or its canary is missing,
   stop with exit 2 before the SDK or publication.
6. Rebuild the controller's own `os.environ` to a positive allow-list before the worker
   exec; at minimum remove `ARB_MEMORY_*`, `AGENT_REDIS_*`, `ANTHROPIC_*`,
   `CLAUDE_CODE_*`, `OTEL_*`, proxy, token, and `SSH_AUTH_SOCK` keys. Start the worker
   with `start_new_session=True`; record PID, PGID and a random worker
   nonce in a controller-owned run journal. Use an `exec` boundary and a positive
   allow-list child environment. The worker returns only non-secret session metadata and
   leaves the decision record in the workspace.
   It inherits only fds 0/1/2 plus explicitly documented IPC: launch with
   `close_fds=True`, and make the controller's env-file fd close-on-exec.
7. Whether the SDK succeeds, errors, or times out, close IPC, send SIGTERM to the stored
   process group, wait a bounded grace interval, send SIGKILL, `waitpid`, and perform the
   platform survivor proof. A teardown failure makes the round fail with no publication.
8. Only after teardown is proven, persist `session-final.txt`, validate
   `decision-record.md`, and enter the existing `DEL -> memory_write -> poll_receipt`
   critical section.
9. Print the final JSON and exit 0 only for the existing successful content-and-receipt
   outcome (`tools/faba/faba_launch.py:393-407`).

The controller's Memory client must not be created until after the worker teardown proof.
This is stricter than today's creation immediately before deletion
(`tools/faba/faba_launch.py:190-206`) and reduces accidental descriptor inheritance.

### Hole 1 — deny-by-default mediation

#### Manifest semantics

`manifest.json` remains the declared, hashed source of the ceiling. Its
`allowed_tools` values become **ceiling membership**, not values passed to SDK
`allowed_tools`. `disallowed_tools` remains auditable policy input, but every effective
denial is implemented by the callback and/or OS boundary; it is not trusted as the sole
enforcement path. In particular, `Bash(git commit:*)` pattern interpretation is not
trusted: the callback command-text check is authoritative, and a live probe must prove
`git commit` is rejected. Startup rejects unknown tools, duplicates, an empty ceiling,
wildcard entries, contradictory allow/deny entries, and allow-lists `permission_mode`
to exactly `{"default"}`; `auto`, `acceptEdits`, `bypassPermissions`, `plan`, and
`dontAsk` are rejected. The manifest version must bump because this is a semantic
change, and future version changes require an explicit migration rule.

The intended FABA ceiling is:

- allow broad `Read`, `Grep`, and `Glob` over the non-secret readable repo view;
- allow `Write` and `Edit` only when the canonical destination is beneath the one round
  workspace;
- allow Bash for verify-don't-trust work, inside the Bash sandbox and outer filesystem
  boundary;
- allow only the explicitly configured local read-only Memory MCP tools already named
  in the manifest (`tools/faba/manifest.json:11-13`), when that server is present;
- deny Task, WebFetch, WebSearch, all unknown tools, and all mutation-capable MCP tools;
- deny git commit, push, merge, rebase, tag, checkout/switch with mutation, and remote
  mutation. The callback command check is authoritative for git-command mediation;
  the secret-omitting read-only repo view and Seatbelt write denial are authoritative
  for filesystem effects, and deny-default outer networking that allow-lists only the
  destination-pinned loopback broker is the primary receipt-forgery control.

#### SDK options

The worker builds options from the bridge helper rather than reproducing its three
critical literals:

```python
kwargs = gated_option_kwargs()  # default, [], []
ClaudeAgentOptions(
    **kwargs,
    can_use_tool=faba_gate,
    cwd=workspace,
    add_dirs=[sanitised_read_only_source_root],
    env=scrubbed_worker_env,
    strict_mcp_config=True,
    mcp_servers=explicit_read_only_servers,
    sandbox={
        "enabled": True,
        "autoAllowBashIfSandboxed": False,
        "excludedCommands": [],
        "allowUnsandboxedCommands": False,
        "network": {
            "deniedDomains": ["*"],
            "allowUnixSockets": [],
            "allowAllUnixSockets": False,
            "allowLocalBinding": False,
            "allowMachLookup": [],
        },
    },
)
```

`autoAllowBashIfSandboxed` is false because automatic Bash approval would recreate an
ask-path bypass. `excludedCommands` is empty and unsandboxed commands are disabled.
`SandboxSettings.deniedDomains` is orthogonal WebFetch policy, not the Bash network
gate; the outer Seatbelt/cgroup boundary is authoritative for worker access to the bus
and broker. Socket and Mach SDK controls remain pinned defence in depth, while Web tools
are denied by policy. The Claude CLI's sole legitimate network destination is the
loopback provider-credential broker, so the outer boundary denies network by default
and allow-lists only that exact localhost host/port.

The worker environment follows the reason behind `isolated_env`: the SDK builds
`process_env = {**inherited_env, **options.env}` from the parent environment
(`/Users/<user>/<workspace>/.venv/lib/python3.14/site-packages/claude_agent_sdk/_internal/transport/subprocess_cli.py:430-434`),
so an options overlay cannot scrub the controller's launch-time environment. The
controller therefore rebuilds its own environment before exec, and the worker gets a
positive allow-list with a fresh private `CLAUDE_CONFIG_DIR`. A startup proof inspects
the real process environment (on Linux, `/proc/self/environ`) and fails if any
Memory/bridge/provider secret remains. No account, user, project, or local settings are
loaded because `setting_sources=[]`.

No provider secret may enter that environment: `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_API_KEY`, and every provider token/key are absent. The only auth-adjacent
worker key is the base-url pointer, for example
`ANTHROPIC_BASE_URL=http://localhost:<broker-port>`. The loopback credential broker
appends the authentication header outside the worker boundary and is a
destination-pinned reverse proxy: it forwards only to the required provider domain and
is never forward- or CONNECT-capable. Until a provider-specific broker, a live Bash
non-disclosure probe, and a probe that the bus cannot be reached through the broker all
pass, that engine—including non-frontier engines—is not launchable.

`strict_mcp_config=True` prevents ambient MCP discovery. Only explicitly constructed
read-only server definitions are supplied. If `ARB_MEMORY_LOCAL_MCP` is unset while the
manifest names its tools, startup fails closed. Failure while constructing them is fail
closed if the manifest names those tools. The current suppression site silently falls
back to `{}` (`tools/faba/faba_launch.py:243-250`); PF3 replaces that behaviour because
it would make the declared ceiling and actual surface diverge.

`ARB_MEMORY_LOCAL_DSN` is a separate read-side Memory credential supplied to the local
MCP subprocess (`src/agent_redis_bridge/local_memory_mcp.py`), not the write-bus URL.
The recommended first PF3 slice removes the three local Memory tools, preserving the
invariant that no process inside the worker boundary receives a Memory credential. They
may return only as a separately approved extension with their own read-only ACL/separate
database and proof that the MCP cannot write or expose a reusable bus credential. The
worker module must also be isolated from the repository `src/` import path so it cannot
transitively import `arb_memory.bus`.

#### Callback policy and startup proof

For tools that reach the SDK ask path, the callback returns an SDK
`PermissionResultAllow` only when all applicable predicates pass. Auto-approved read
tools are not observed by this callback; that is benign only while the ceiling permits
them and the OS filesystem view remains authoritative.

- exact tool name is in the parsed ceiling;
- the tool is a known built-in or exact explicitly configured MCP read tool;
- Write/Edit destination canonicalises beneath the workspace, with an `openat`/no-follow
  style final write or equivalent OS boundary to defeat symlink swaps;
- Bash input is structurally valid, contains no explicit forbidden operation, and is
  subject to the Bash sandbox; and
- no exception occurred. Every exception returns `PermissionResultDeny` and a scrubbed
  audit reason, following the bridge's fail-closed callback
  (`src/agent_redis_bridge/engines/agent_sdk.py:347-382`).

Before a real prompt, the worker must:

1. assert the complete security-relevant option set: `permission_mode == "default"`,
   `allowed_tools == []`, `setting_sources == []`, `hooks` empty, `agents is None`,
   `strict_mcp_config is True`, the exact callback identity, no unsandboxed commands,
   and only the explicit MCP definitions;
2. assert the connected SDK query holds the identical callback, as the bridge does at
   `src/agent_redis_bridge/engines/agent_sdk.py:401-405`;
3. call the callback with one read-only in-ceiling tool and require Allow;
4. call it with `FABA_STARTUP_DENY_SENTINEL` and require Deny, matching the bridge's
   deterministic allow/deny checks (`src/agent_redis_bridge/engines/agent_sdk.py:416-427`);
5. run one live read-only LS/Glob probe and require successful confinement, but not a
   callback audit record: read-only tools can auto-approve before the ask path. The
   bridge likewise logs both live-smoke branches rather than requiring that its gate
   fire (`src/agent_redis_bridge/engines/agent_sdk.py:429-471`). Deterministic callback
   proof remains in steps 2-4; a separate live `Bash echo` ask-path probe may be used
   when implementation accepts its controlled scratch litter.

Any failure exits the worker and prevents publication. The manifest hash and effective
ceiling are recorded without secrets in the final controller JSON. Tests pin the entire
asserted option set so a newly introduced mode such as `auto` cannot silently bypass it.

### Hole 2 — credential unreachability

The core rule is **absence from the worker's data plane plus a kernel check**, not
parse-don't-export alone.

#### Common controls

- The env file is opened only by the controller, using a canonical absolute path and a
  no-follow, close-on-exec open. The worker never receives that path or an inherited fd
  for it; it inherits only fds 0/1/2 and explicitly documented IPC.
- The authoritative readable source is a read-only generated view that omits secrets:
  working-tree files plus only `HEAD`-reachable objects, with no reflog, stash, other
  worktree metadata, or secret-bearing untracked files. A partial clone or equivalent
  bind-mount construction supplies that view; the checkout is never mounted wholesale.
- Whole-history byte scanning is advisory defence in depth because encoding defeats an
  exact grep. When used, it covers dangling/unreachable objects via
  `git cat-file --batch-all-objects --unordered`, all reflogs, stash, worktrees,
  submodules, `objects/info/alternates`, and `--separate-git-dir`. Any alternates entry
  or git-dir target outside the worker-readable mount rejects launch. Before a real
  round, a negative-control fixture plants both plain and encoded fake credentials and
  requires the scanner to find them; scan cost is confined to source-view construction,
  not repeated over irrelevant host history on every run.
- The controller never receives a bus/provider secret in its initial environment and
  rebuilds its environment before exec. The worker environment is a positive allow-list.
  `/proc` exposure on Linux must not allow reading controller argv/environ; the
  processes are in separate PID/user namespaces in the container form.
- The repo view is read-only to the worker; the round workspace is the only durable
  writable tree. Provider config/cache paths are private and minimal; authentication is
  supplied through the loopback broker rather than files or environment.
- The controller performs a negative read canary through the **same worker boundary** on
  every run before starting the model. A denied canary is required; inability to execute
  the probe is not a pass.

#### macOS fleet host

The recommended design drops the SDK worker to a dedicated per-round UID. A narrowly
scoped setuid helper performs the UID switch and the worker inherits that UID; the SDK
`user` field is unset or merely re-affirms the inherited UID. No untrusted worker code
holds `CAP_SETUID` or setuid privilege. The helper later reaps the UID, and the whole
worker/SDK CLI tree—not just individual Bash calls—is wrapped in an outer Seatbelt
profile. Use a canonical-path profile with normal toolchain reads, kernel-side
`deny process-info` intent that must pass a fleet-version canary probe, explicit
`deny file-read*` rules for the env file and controller-owned secret/canary directory,
default-deny writes with an allow for the workspace **canonical realpath only**, and
denial of access to controller IPC after result closure. Do not allow the broad
`/private/var/folders/*` temp root: concurrent round workspaces persist and could
cross-read. Canonicalisation is load-bearing: `/tmp` versus `/private/tmp` can otherwise
silently mis-scope a rule. Do **not** follow the diagnose skill's global
`/private/var/folders` temp-allow pattern (`skills/diagnose/containment.py:17-31`); the
per-workspace canonical-realpath allow must be live-proven. Existing host evidence shows an
allow-default profile with a targeted canonical read denial can stop absolute, alias,
and symlink reads, but needs a per-run canary and readable-surface scan
(`docs/eval-seat-confinement.md:67-78`).

This deliberately does not rely on an SDK Read denial to stop `Bash cat`: the whole
worker tree receives the kernel denial. Write/Edit callback path checks remain useful
diagnostics, while Seatbelt is authoritative for workspace-only writes. The
`deny process-info` rule expresses kernel-side intent to block `ps`/`sysctl` inspection
of the controller's launch-time environment, but cannot be claimed until its canary
passes on the fleet's Seatbelt version. For TCP, the outer profile denies network by
default and allow-lists only the exact loopback broker host/port required by the CLI;
the broker is the worker's sole network destination. For a Unix-socket Redis bus, the
profile also denies its filesystem path and the secret-omitting readable view excludes
it. The required macOS deny-default-plus-single-loopback-allow pattern is a hard slice-2
launch gate: the macOS slice is not certifiable until its live probe passes, including
when the bus also listens on localhost. Seatbelt plus PGID without the dedicated UID
remains a weaker fallback, with the detached-survivor residual named under Hole 3.

#### Managed/Hetzner Linux

Run the worker in a mount-minimal container/user namespace:

- mount a generated read-only source view that omits the env file and all secret-bearing
  untracked files (do not bind the checkout wholesale);
- mount only the round workspace read-write;
- expose only the destination-pinned loopback credential broker needed by the CLI,
  with the key outside the worker namespace and deny-default networking around it;
- no Memory-bus secret, socket, env file, host Redis socket, Docker socket, SSH agent, or
  host `/proc` view;
- a dedicated cgroup for the whole round and a deny-by-default seccomp/capability posture.

This is the preferred production boundary because the env file is absent rather than
hidden by a path rule. If the loopback broker cannot keep provider credentials out of
Bash's environment, that is a separate secret-containment blocker for launch, even
though it is not the Memory receipt-forgery credential.

### Hole 3 — teardown before receipt gating

The primary receipt-forgery control is deny-default networking for the entire worker OS
boundary, with only the destination-pinned loopback provider broker allow-listed.
Receipt keys are deterministic, but neither an ordinary worker nor a survivor can LPUSH
to a bus it cannot reach. For a Unix-socket bus, exclusion and denial of the socket path
is part of the same outer boundary. This control is load-bearing regardless of bus
authentication; authenticated transport is useful additional depth when available.
Launch requires proof that the broker is reachable, the bus is not, and the broker
cannot tunnel or proxy a request to the bus endpoint.

The controller starts the exec-isolated worker with `start_new_session=True` and stores
the returned PGID before accepting worker output. The SDK CLI and ordinary Bash
descendants inherit that group. The worker is the PGID/session leader and launches only
the CLI; no controller helper or broker shares its group. On every exit path the controller performs
TERM -> bounded grace -> KILL -> wait, using the stored PGID, and proves the boundary
empty before it calls `publish_and_gate`.

The ordering invariant is:

```text
worker running
  -> worker result/EOF
  -> TERM/KILL containment boundary
  -> wait + survivor proof
  -> validate decision-record.md
  -> DEL receipt key
  -> publish
  -> poll genuine receipt
```

There must be no callback, signal handler, asynchronous cleanup task, or record
validation path that can publish before the survivor proof. `publish_and_gate` should
accept a controller-owned teardown token/attestation (or be called only inside a state
machine state that cannot be reached otherwise) so a future refactor cannot accidentally
restore today's direct call.

#### macOS

A POSIX process group alone is insufficient because a deliberate child can call
`setsid()`. The recommended macOS slice therefore combines Seatbelt and PGID teardown
with a dedicated per-round UID. A narrowly scoped setuid helper creates/selects the UID,
launches the worker under it, then performs `pgrep -U` census and `pkill -U` teardown
until the UID is empty before publication. This closes the detached-session escape
without requiring a container; failure to obtain or prove the UID boundary fails closed.

Seatbelt plus PGID is a weaker fallback, not the recommended closure. Its named residual
is that a deliberately detached process may survive. Such a process is denied the bus
endpoint and secret-bearing files, but that is a receipt-capability argument—not proof
of zero descendants. A worker nonce scan is additional detection only. Plain `killpg`
must never be described as absolute descendant containment.

#### Managed Linux

The survivor proof and kill target are the dedicated round cgroup/container, which
includes double-forked and `setsid` descendants. Publication requires both the worker
wait and `cgroup.events populated=0`; the worker receives a parent-death signal as an
additional crash control.

On controller crash there is no publication because only the dead controller held the
credential. The service manager or a small supervisor must invoke the recorded cgroup
or per-round-UID cleanup. The run journal lets
the next launch clean and prove stale boundaries before starting another round. A stale
worker blocks later publication; it is never ignored.

The current receipt gate binds on `artefact_id`. The receipt key is deterministic from
`request_id` (`src/arb_memory/bus.py:34-35`). The worker derives
`request_id = f"faba-r{round}-{run_id}"` from the round number and
`record_artefact_id = f"art-faba-{run_id}"` in `round-input.json`, and the request id is
also printed to harness stderr (`tools/faba/faba_launch.py:322-323`,
`tools/faba/faba_launch.py:336-347`, `tools/faba/faba_launch.py:368`). The current
store result contains only
`artefact_outcome`, `artefact_id`, `version`, and `hints_stored`
(`src/arb_memory/store.py:169-173`); `_publish_result` pushes that receipt plus
`duplicate`, while using `request_id` only for the key
(`src/arb_memory/bus.py:190-196`); and `gate_decision` checks only
`artefact_outcome` and `artefact_id` (`tools/faba/faba_launch.py:149-164`).
Receipt-envelope ULID binding is therefore contingent on an `arb_memory` consumer
change: `_publish_result` must echo `intent['ulid']` into the envelope. PF3 does not
include that consumer change. Until it exists, the receipt gate binds on `artefact_id`
and relies on the worker OS boundary's bus-endpoint denial as the primary forgery
defense.

Two named future hardenings remain worthwhile belt and braces, not PF3 dependencies:

- **Receipt-envelope ULID echo and binding:** after `_publish_result` echoes the intent
  ULID, capture the controller's publish ULID and require the receipt to match it. This
  is a future belt after the platform's deny-default network launch gate passes, not a
  substitute for proving that only the loopback broker is reachable.
- **Store fetch-by-id read-back:** after receipt, fetch the exact artefact/version and
  compare its content digest and bindings with the validated record when that API
  exists.

## Cross-cutting requirements

### The subagent form and the shared instruction surface

Both forms continue to consume `round-contract.md`; the SDK composes it into the
bootstrap template (`tools/faba/faba_launch.py:52-64`), while the subagent driver embeds
it in its brief (`tools/faba/subagent/run_probe_round.py:108-139`). PF3 changes execution
containment, not that instruction surface. No containment policy should be duplicated in
the contract: kernel and callback rules are not agent instructions.

The subagent form is more tool-bounded by construction: its agent definition declares
exactly Read, Grep, Glob, Write, Edit, and Bash (`.claude/agents/faba-round.md:1-7`), and
live evidence reported unavailable discovery/Web tools (`tools/faba/subagent/README.md:74-82`).
However, its outer probe driver currently launches a child with non-empty
`--allowedTools Task,Read,Grep,Glob,Write,Edit,Bash`
and no `can_use_tool` callback (`tools/faba/subagent/run_probe_round.py:243-261`), so
that raw allow-list is the subagent form's only Hole-1 mediation and retains the
additive-approval surface PF3 warns about. Its own README admits the same
on-disk credential-harvest residual (`tools/faba/subagent/README.md:53-72`). Thus this
SDK-path design does not silently claim that the subagent form is fully PF3-contained.
It remains a separate execution path whose declared agent list narrows Hole 1, but it
must adopt the same secret-free filesystem boundary and descendant teardown before its
driver publishes if it is promoted beyond prototype. `round-contract.md` remains the one
shared instruction surface either way.

### Tests: production composition, never injected content

Tests must exercise production-created bytes. The subagent path already exposes
`build_brief` and `materialise_workspace`, and the existing structural tests call those
functions (`tools/faba/tests/test_subagent_gate.py:281-342`). The SDK refactor must expose
equivalent production helpers, and PF3 tests must call them with the real
`round-contract.md`, `bootstrap_template.md`, manifest, round input, and prior-record
flow. A test may substitute Redis/SDK transport at a boundary, but may not inject a
handwritten prompt, decision-record content, or options object in place of what
production composes. This rule is load-bearing because a correct gate around the wrong
payload is still a failed round.

Required deterministic coverage includes:

- manifest parsing, explicit version migration, and rejection cases; effective SDK
  options equal the complete pinned set (`default`, empty allowed/settings/hooks,
  `agents=None`, strict MCP, exact callback and sandbox policy), including rejection of
  `auto` and every other permission mode;
- callback allow/deny/error cases, including canonical workspace paths, `..`, absolute
  outside paths, symlink swap, unknown tools, mutation MCP, forbidden git mutations, and
  Bash unsandbox escape requests;
- controller initial-environment gate rejects Memory/provider secrets; child process
  environment (including `/proc/self/environ` on Linux) has no Memory/bridge/provider/
  proxy/token/SSH secret, specifically no `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_API_KEY`, or provider secret; the only auth-adjacent value is the pinned
  localhost base URL, loaded setting sources are empty, and only declared fds remain;
- outer deny-default networking allows only the exact loopback broker host/port and
  denies the resolved TCP bus endpoint, including a localhost-bus fixture; a Unix-socket
  bus path is unreadable and denied; the broker cannot forward/CONNECT or tunnel to the
  bus; authentication, when present, is verified only as additional depth;
- controller never passes env-file path/credential/receipt key into worker argv, env,
  prompt, IPC, workspace, session final, or logs;
- real OS canary denied, workspace read/write allowed, repo write denied, provider/model
  startup through the credential broker still works, the bus endpoint is unreachable,
  process-info is denied on macOS, and `/tmp` plus `/private/tmp` aliases cannot escape;
- the generated source view omits secrets, reflog/stash/worktree/alternates/submodule
  surfaces; scanner negative controls detect planted plain and encoded credentials;
- `git commit` is rejected by the live callback path regardless of
  `disallowed_tools` pattern semantics; `ARB_MEMORY_LOCAL_MCP` absence fails closed;
- normal, error, timeout, SIGTERM-resistant, ordinary background, double-fork, and
  `setsid` descendant cases; macOS per-round-UID and Linux cgroup census are empty;
  publication mock asserts teardown attestation precedes DEL;
- receipt polling retains the current `artefact_id` binding; the forged-different-ULID
  rejection test is future-hardening coverage gated on the `arb_memory` consumer first
  echoing `intent['ulid']` into the receipt envelope;
- controller crash recovery and stale-run journal cleanup; and
- existing schema, receipt, succession, and shared-contract tests remain green.

### Non-frontier engines

PF3 is a gate for non-frontier engines, so containment cannot assume that the model
obeys the prompt, voluntarily avoids denied tools, reports its actions honestly, or
stops spawning processes when its final message is emitted. Tests use adversarial tool
inputs directly; model compliance is irrelevant to pass/fail. Model selection may alter
only provider endpoint/auth and model id. It may not alter the empty SDK allow-list,
settings isolation, manifest ceiling, OS mounts/rules, teardown state machine, or
publication ordering.

Adding a non-frontier model also requires a provider-specific loopback broker and proof
that its credential is unavailable to Bash while its endpoint works through the chosen
sandbox. The bridge's current model environment builder inserts provider credentials
into the SDK environment
(`src/agent_redis_bridge/engines/agent_sdk_models.py:111-122`); FABA must not copy that
pattern. Until the broker and provider-secret probe pass, that engine is not launchable
through FABA.

### Verification and executable escape probes

The implementation should add a single probe runner whose cases invoke the real
materialisation and options factories. These are the required certification commands
and expected exits (commands are prospective; the files do not exist in this design-only
change):

```sh
# Deterministic suite, including production-composed brief/payload and state ordering.
/Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/ -q
# expected: exit 0

# Hole 1: seed a user/account pre-approval, then request an outside-ceiling sentinel.
# Pass only if allowed_tools/settings_sources remain empty, the callback is observed,
# the sentinel is denied, and an in-ceiling read succeeds.
/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py \
  --case additive-preapproval --sdk-version 0.2.107
# expected: exit 0; escape attempt denied; callback audit contains the sentinel

# Hole 2: place a unique fake Memory URL in the real --env-file, then attempt Read,
# Bash cat, Python open, symlink and /tmp-/private/tmp alias reads through the real worker.
# Pass only if all fail and the sentinel appears in no worker output or readable copy.
/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py \
  --case credential-harvest --host macos
# expected: exit 0; every harvest path denied, normal repo read + workspace write pass

# Hole 3 on macOS: create ordinary, SIGTERM-resistant, double-fork and setsid descendants
# and attempt a delayed receipt re-seed. Pass only after PGID reap, UID-wide census/kill,
# bus-endpoint denial, and a receipt matching the controller's artefact id.
/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py \
  --case descendant-reseed --host macos
# expected: exit 0; per-round UID survivor count 0; forged receipt cannot reach the bus

# Hole 3 on managed Linux: add double-fork + setsid descendants. The cgroup/container
# must be empty before DEL, and the forged receipt must never appear.
/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py \
  --case descendant-reseed --host linux-container
# expected: exit 0; cgroup.events populated=0 before DEL; survivor count 0
```

Each probe writes a machine-readable report containing SDK/CLI version, manifest and
profile digests, canonical paths, tool decisions, PGID/cgroup identity, ordered teardown
and Redis events, and redacted result digests. Exit 0 means the attempted escape was
observed and blocked. A skipped OS probe, absent executable, missing Redis fixture,
unseen callback, or model that simply chose not to attempt the escape is non-zero, not
SKIP/PASS. The additive and credential probes must also run live against the installed
CLI on every SDK/CLI upgrade; mocked callback tests are necessary but insufficient.

The implementation plan must additionally pin these live cases: reject unknown manifest
versions; reject a missing local-MCP executable when its tools are declared; prove only
fds 0/1/2 plus declared IPC survive exec; prove the fleet's `deny process-info` canary;
verify `deniedDomains` only as orthogonal WebFetch policy and pin explicit socket/Mach
settings; deny both `/tmp` and
`/private/tmp` aliases; prove the workspace allow is its canonical realpath rather than
the shared `/private/var/folders/*` tree; and prove the worker is the session/PGID leader
with no controller helper in its group. It must also hard-gate macOS launch on the
deny-default network profile allowing only the loopback broker while denying a
separately bound localhost bus, and prove the broker cannot tunnel to that bus.

## Implementation slice ordering

The first launchable PF3 slice is ordered by prerequisites, not merely by controller
code dependencies:

1. Build and live-probe the provider-specific loopback credential broker for every
   engine in scope. The destination-pinned reverse proxy must keep the provider key
   outside the worker data plane, expose no forward/CONNECT path, and successfully carry
   CLI traffic through its exact localhost host/port; a survivor must be unable to reach
   the bus through it. No engine can launch before this infrastructure exists.
2. Build the secret-omitting source view and platform OS boundary, including the
   per-round UID/Seatbelt path on macOS or namespace/cgroup path on managed Linux, then
   prove credential-read denial and deny-default networking while retaining only broker
   access. The macOS slice cannot proceed until the live deny-default-plus-broker-allow
   profile passes against both remote and localhost TCP bus fixtures and any configured
   Unix-socket path is excluded and denied.
3. Split controller and worker exec images, add the deny-by-default SDK mediation and
   startup proofs, and enforce teardown attestation before publication.
4. Run the deterministic suite and applicable live certification probes. ULID receipt
   binding and store fetch-by-id remain future hardenings, not blockers for this slice.

## Open questions and named residuals

1. **Weaker macOS fallback residual.** The recommended first PF3 slice uses a dedicated
   per-round UID and has no accepted `setsid` residual. If operations cannot supply the
   setuid helper, Seatbelt + PGID may be evaluated separately, but its detached survivor
   residual requires explicit owner acceptance and must not be called absolute
   descendant containment.
2. **Seatbelt profile and network launch gate.** The exact Claude SDK/CLI runtime paths,
   nested Bash-sandbox interaction, workspace-realpath rule, process-info rule, and
   deny-default network profile need live proof. This is a launch blocker, not an
   advisory spike: the macOS slice is not certifiable until the per-run canaries, real
   SDK smoke, and single-broker-allow network probe all pass.
3. **Local Memory read MCP extension.** The first slice removes the three tools. The
   owner may later approve their return only after the local DSN has a separate read-only
   ACL/database, strict MCP config, read-only method validation, and proof that its
   transport conveys no reusable write credential.
4. **Crash supervisor on macOS.** The implementation plan must select launchd ownership,
   a tiny supervisor, or another controller-death cleanup mechanism. The invariant is
   fixed (stale UID boundary blocks later publication); only the operational mechanism is
   open.
5. **Bus transport assumption.** TCP workers receive deny-default networking with only
   the loopback provider broker allow-listed; the macOS profile must prove this even
   when Redis also listens on localhost. A Unix-socket bus is instead a filesystem
   object: its path must be absent from the readable view and explicitly denied. Failure
   of either platform-specific probe blocks launch. Future receipt-envelope ULID binding
   remains belt-and-braces hardening rather than a PF3 dependency.
6. **Future receipt hardening.** Receipt-envelope ULID echo/binding and fetch-by-id
   digest/binding verification are named future hardenings, not substitutes for the
   deny-default outer boundary and not prerequisites for this design.

The implementation may not claim PF3 complete until all four certification commands
pass on their applicable hosts and every accepted residual has an explicit owner decision.

## Panel fold log

- **MF1:** identified the deterministic receipt-key forgery vector and required worker
  denial of the resolved bus endpoint; the v3 owner folds below supersede its bus-auth
  precondition and immediate ULID-binding proposals.
- **MF2:** barred secrets from the controller's initial environment, required pre-exec
  environment rebuilding/startup proof, and added macOS `deny process-info`.
- **MF3:** promoted dedicated-per-round-UID census/kill to the recommended macOS slice.
- **MF4:** corrected the transposed SDK launch and cleanup citations.
- **MF5:** removed the impossible read-only live-probe audit requirement and scoped the
  callback predicate to ask-path tools.
- **MF6:** expanded the pinned option proof to exact default mode, empty hooks, and no agents.
- **SF1:** required close-on-exec/`close_fds`, minimal fd inheritance, and worker import isolation.
- **SF2:** made the secret-omitting source view authoritative and expanded the advisory
  scanner plus encoded negative control.
- **SF3:** selected a loopback provider credential broker and separately scoped the local
  read-MCP DSN.
- **SF4:** pinned socket/Mach sandbox controls and restated FS, credential, and bus-network
  authorities accurately.
- **SF5:** made callback command inspection authoritative for git mutation and added a live probe.
- **SF6:** split Hole 2 and Hole 3 explicitly into macOS and managed-Linux subsections.
- **F11:** pinned both temp aliases and kept `add_dirs` subordinate to Seatbelt.
- **F12:** required explicit manifest-version migration.
- **F13:** made missing declared local-MCP configuration fail closed.
- **F14:** restricted workspace access to its canonical realpath, not the shared temp root.
- **F15:** required the worker alone to lead the CLI process group/session.
- **FoldA:** made receipt-envelope ULID binding contingent on a future `arb_memory`
  consumer echo and retained it beside fetch-by-id as named future hardening.
- **FoldB:** removed the authenticated-bus launch precondition, promoted worker
  bus-endpoint network denial to the primary forgery control, and named macOS
  host/port-filter granularity as a residual.
- **FoldC:** made a live provider-specific loopback broker the first-slice prerequisite
  for every engine.
- **Fold1:** corrected the deterministic `request_id` disclosure path to
  `round-input.json`'s record artefact id plus round number and harness stderr, removing
  the false tempdir-prefix mechanism.
- **Fold2:** inverted the outer worker network control to deny-default with only a
  destination-pinned, non-tunnelling loopback broker allowed; pinned provider-env and
  dedicated-UID privilege contracts, covered Unix-socket buses, and made the macOS live
  network probe a hard slice-2 launch gate.
- **Fold3:** corrected current/future and canonicalisation citations, documented the
  subagent form's raw allow-list mediation gap, and made `deny process-info` a
  canary-gated kernel-side intent.

# The comms plane — `agent_redis_bridge`

The bridge daemon and its operator surface. One process per engine × project × workspace: it
registers on the bus, BLPOPs its inbox, runs the engine, and replies to the sender. The main
entry point is `agent-redis-bridge`; `codex-redis-bridge` remains as a compatibility alias
that defaults to the Codex engine.

This file is the **operator/reference** layer. The orchestrator-facing view — when to dispatch,
how to monitor, how to run a panel — is [`../../skills/using-agent-bridge/SKILL.md`](../../skills/using-agent-bridge/SKILL.md);
the wire contract is [`../../SPEC.md`](../../SPEC.md); the engine adapters are
[`engines/README.md`](engines/README.md).

## Quickstart (clone anywhere)

The bridge is config-driven; everything you need lives in `.env.example`. To pair a fresh repo
with this bridge:

```bash
# 1. Reachable Redis (any version that supports BLPOP/HSET — i.e. anything modern)
redis-server --port 6390 &

# 2. Engine CLI installed + authed (one or more)
npm i -g @openai/codex && codex login
# NB: @google/gemini-cli is DEPRECATED (2026-07-03, killed by Google) — gemini-acp no longer works.
# grok (the local Grok Build TUI) is used via `grok agent stdio` — log in once with the TUI or OAuth

# 3. Copy .env.example into your *application* repo's worktree and edit
cp <bridge-clone>/.env.example <app-worktree>/.env
${EDITOR:-vi} <app-worktree>/.env
#   Set at minimum: AGENT_PROJECT, AGENT_WORKSPACE, AGENT_WORKDIR,
#                   AGENT_TRUSTED_SENDERS, AGENT_DISPATCH_FROM

# 4. Start a bridge daemon (manual for a foreground diagnostic, or via the supervised
#    systemd/launchd pattern for a durable seat; nohup is not supervision)
AGENT_ENV_FILE=<app-worktree>/.env \
AGENT_WORKDIR=<app-worktree> \
AGENT_PROJECT=<project> \
AGENT_TRUSTED_SENDERS=claude-<project>-dev=trusted,human-codexctl=human \
PYTHONPATH=<bridge-clone>/src python3 -m agent_redis_bridge \
  --engine codex --workspace dev
```

Every config knob is documented in `.env.example`. The dispatcher and the systemd wrapper read
from the same file, so a single `.env` per worktree is enough. Step 5 — the dispatch itself --
is the canonical recipe below, not a bare `agent-dispatch` call: free-form positional task
strings were removed in Slice 1d-iv.

## Canonical dispatch recipe

Use this shape for non-trivial peer-agent work; write longer task bodies to a brief file
and pass the brief path.

<!-- fragment:dispatch-recipe begin -->
```bash
# Slice 1d-iv: ordinary dispatch is store-before-send via dispatch_authority.
# 1) Short-lived FABA driver publishes the brief (holds ARB_MEMORY_REDIS_URL):
/<bridge-clone>/scripts/arb-memory-harness-publish \
  --target-agent-id codex-<project>-<workspace> \
  --brief <brief-path> \
  > /tmp/<task>.receipt.json
# 2) Non-FABA enqueue (no publish credential) through the single authority:
FROM_AGENT_ID=claude-<project>-<workspace> \
BRANCH=<your-current-branch> \
AGENT_ENV_FILE=<path-to-the-app-worktree>/.env \
env -u ARB_MEMORY_REDIS_URL \
/<bridge-clone>/scripts/dispatch-dev \
  --engine codex \
  --target-id codex-<project>-<workspace> \
  --timeout 5400 \
  --run-id "$RID" \
  --artefact-id "$(jq -r .artefact_id /tmp/<task>.receipt.json)" \
  --version "$(jq -r .version /tmp/<task>.receipt.json)" \
  --receipt /tmp/<task>.receipt.json \
  --brief <brief-path> \
  > /tmp/<task>.out 2> /tmp/<task>.err
```

`dispatch-dev` wraps the Go client edge (`tools/go-client`, auto-built on first use;
`USE_BASH_DISPATCH=1` falls back to the raw Python `scripts/agent-dispatch`) and
AUTO-DEFAULTS a meaningful `--run-id` (from the `--brief` path slug, or
`<target>-<branch>-<HHMMSS>`) when one isn't given — so it never hits the
`--run-id`/`--adhoc` hard-refuse the raw `agent-dispatch`/`go-client` binaries enforce as of
2026-07-01. Ordinary request/worktree_run **must** pass the pre-minted
`--artefact-id`/`--version`/`--receipt`/`--brief` quartet; free-form positional task
strings were removed in Slice 1d-iv (enqueue only via `dispatch_authority.publish_and_enqueue`).
Still mint one yourself for a panel/multi-round workflow —
`RID=panel-<slug>-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)`, reused verbatim on
every seat in that round — because the auto-default is per-call (different target/timestamp
per seat unless they share one brief path), so it won't group a multi-seat
panel under one label on its own. See "Auditing a review/design panel" below.
<!-- fragment:dispatch-recipe end -->

The "Auditing a review/design panel" section the fragment points at lives in
[`../../skills/using-agent-bridge/SKILL.md`](../../skills/using-agent-bridge/SKILL.md) --
the fragment is shared verbatim with that file by `scripts/check-doc-drift`, so the pointer is
written from the skill's vantage.

<!-- fragment:env-overrides begin -->
| Override | Why it's needed |
|---|---|
| `FROM_AGENT_ID` | The bridge's `--sender-policy` only trusts specific agent IDs. The shell helper's default may be a legacy value, so supply the real ID. |
| `BRANCH` | The bridge rejects empty branches as `envelope-invalid invalid-branch`. In detached HEAD, `git branch --show-current` returns `""`. |
| `AGENT_ENV_FILE` | Points helper scripts at the correct Redis and project settings for this worktree. |
| `--target-id` | Overrides legacy or inferred target names; use the actual registered agent ID. |
| `--timeout` | Default is 1800 seconds. Use 5400+ for substantial review or implementation tasks. |
| `--turn-timeout` | Optional ceiling for one task engine turn, not total multi-turn dispatch duration. Trusted senders may request above or below the seat default, up to its `--turn-timeout-max`; keep client `--timeout` above it. |
<!-- fragment:env-overrides end -->

### Role profiles

Set `BRIDGE_ROLE_PROFILE_FILE=<bridge-clone>/roles/reviewer.md` to attach a seat-level role
profile to every request handled by that bridge. Pi engines consume the file through their
native role-profile path. Codex and the live non-pi engines receive the same content as a
first-turn `<system_guidance>` prompt wrapper, so the profile reaches all live engines without
double-injecting pi seats. Historical note: `gemini-acp` followed this path before its
2026-07-03 deprecation. The profiles themselves are catalogued in
[`../../skills/README.md`](../../skills/README.md) § "Role profiles".

Upgrade note: the bridge no longer falls back to hardcoded per-host defaults when
`AGENT_WORKDIR` or `AGENT_TRUSTED_SENDERS` are unset. Populate these in your env file, or pass
`--workdir` / `--sender-policy` on the CLI, before upgrading.

If you're cloning the bridge code into a brand-new host, point the systemd unit at it
(`systemd/agent-redis-bridge@.service`'s `WorkingDirectory` + `ExecStart` paths reference the
install location — adjust them).

If you'll be driving the bridge from Claude Code, also install the bundled skill so future
sessions auto-load the operational guide — see [`../../skills/README.md`](../../skills/README.md).

## Run Modes

From this repo:

```bash
cd <bridge-clone>
PYTHONPATH=src python3 -m agent_redis_bridge --self-test
```

`--self-test` registers, heartbeats once, drains any queued inbox messages as `[would-handle]`,
and exits without starting the selected engine.

```bash
PYTHONPATH=src python3 -m agent_redis_bridge --dry-run --once
```

`--dry-run` starts Codex App Server and verifies the connection, but stubs the turn result so no
model tokens are spent. `--once` handles one message and exits.

```bash
PYTHONPATH=src python3 -m agent_redis_bridge
```

Long-lived mode defaults to `--engine codex`, registers as `codex-<project>-dev`, starts
`codex app-server`, BLPOPs `agent_scratch:agent:codex-<project>-dev:inbox`, executes
`kind=request` messages, and replies to the sender inbox.

This persistent mode is the default production mode. Use `--once` only for tests and smoke
checks.

For human terminal chat, allow the terminal sender explicitly:

```bash
PYTHONPATH=src python3 -m agent_redis_bridge \
  --sender-policy claude-<project>-dev=trusted \
  --sender-policy claude-<project>-staging=trusted \
  --sender-policy human-codexctl=human
```

If the host blocks Codex's sandbox setup, start the bridge with the explicit bypass flag instead
of changing host AppArmor:

```bash
PYTHONPATH=src python3 -m agent_redis_bridge \
  --codex-bypass-approvals-and-sandbox \
  --sender-policy claude-<project>-dev=trusted \
  --sender-policy claude-<project>-staging=trusted \
  --sender-policy human-codexctl=human
```

This launches `codex --dangerously-bypass-approvals-and-sandbox app-server ...` and sends
`sandbox="danger-full-access"` in the App Server `thread/start` request. Use it only for trusted
local bridge instances.

Then start the terminal client in another shell:

```bash
PYTHONPATH=src python3 -m agent_redis_bridge.ctl chat
```

Inside chat:

```text
codex> run git status and summarize it
codex> /steer focus only on modified PHP files
codex> /cancel
codex> /status
codex> /result
codex> /quit
```

Non-interactive client commands:

```bash
TASK_ID=$(PYTHONPATH=src python3 -m agent_redis_bridge.ctl send "Run tests and summarize failures")
PYTHONPATH=src python3 -m agent_redis_bridge.ctl watch "$TASK_ID" --from-start
PYTHONPATH=src python3 -m agent_redis_bridge.ctl status "$TASK_ID"
PYTHONPATH=src python3 -m agent_redis_bridge.ctl result "$TASK_ID"
PYTHONPATH=src python3 -m agent_redis_bridge.ctl steer --task-id "$TASK_ID" "skip browser tests"
PYTHONPATH=src python3 -m agent_redis_bridge.ctl cancel --task-id "$TASK_ID"
```

## Systemd

Install the user unit template. The old `codex-redis-bridge@.service` template still works and
defaults to the Codex engine; the new `agent-redis-bridge@.service` template is preferred for new
multi-engine instances. On macOS, use the launchd recipe in
[`../../docs/macos-launchd-seats.md`](../../docs/macos-launchd-seats.md) instead — a plain
`nohup` seat is reaped.

```bash
mkdir -p ~/.config/systemd/user
cp <bridge-clone>/systemd/agent-redis-bridge@.service ~/.config/systemd/user/
cp <bridge-clone>/systemd/codex-redis-bridge@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-redis-bridge@dev.service
```

Role-bearing instances use the same template. The instance name is split on the first hyphen, so
`dev-impl` starts `--workspace dev --role impl` and registers as `codex-<project>-dev-impl`.
The plain workspace-only path remains unchanged:

```bash
systemctl --user enable --now codex-redis-bridge@dev-impl.service
systemctl --user enable --now codex-redis-bridge@dev-rev.service
```

Multi-engine instances can use the new template. The wrapper parses `codex-dev` as
`--engine codex --workspace dev` and `grok-acp-dev` as `--engine grok-acp --workspace dev`
(gemini-acp is deprecated, see [`engines/README.md`](engines/README.md)):

```bash
systemctl --user enable --now agent-redis-bridge@codex-dev.service
systemctl --user enable --now agent-redis-bridge@grok-acp-dev.service
systemctl --user enable --now agent-redis-bridge@codex-dev-impl.service
```

For hosts where AppArmor blocks Codex sandboxing, use a systemd override instead of editing the
unit:

```bash
systemctl --user edit codex-redis-bridge@dev.service
```

Add:

```ini
[Service]
Environment=CODEX_BYPASS_APPROVALS_AND_SANDBOX=1
```

Use the same override path for role-bearing instances. To share usage limits across a role pair,
set a common usage scope:

```bash
systemctl --user edit codex-redis-bridge@dev-impl.service
```

```ini
[Service]
Environment=AGENT_USAGE_SCOPE=codex-<project>-dev-shared
```

Check health:

```bash
<bridge-clone>/scripts/agent-bridge-ping dev
systemctl --user status codex-redis-bridge@dev.service
```

Dispatch a task and wait for its reply. Every target-selection form below is still current —
`--workspace` (with the seat's default engine), an explicit `--target-id`, or `--engine` plus
`--workspace` — but the **task body** is no longer a positional string: it travels as the
pre-minted `--artefact-id`/`--version`/`--receipt`/`--brief` quartet from the canonical recipe
above. Reuse one `RID` across every seat in a round so the panel groups under one label:

```bash
RID=systemd-smoke-$(date -u +%Y%m%dT%H%M%SZ)
# ...then the two-step publish + quartet enqueue from "Canonical dispatch recipe",
# varying only the target selector:
#   --workspace dev
#   --target-id codex-<project>-dev-impl
#   --engine grok-acp --workspace dev
```

Two preflight modes catch a misconfigured dispatch before the round-trip:

```bash
# Validate resolved config (env file, bus, branch, trusted-sender, target heartbeat)
# and exit WITHOUT dispatching. Refuses with a specific message instead of dispatching
# into a sender-rejected / envelope-invalid / wrong-bus failure found only after the LPUSH.
scripts/agent-dispatch --engine codex --target-id codex-<project>-dev --check

# Print the exact JSON envelope that WOULD be LPUSHed, then exit without sending —
# surfaces the \n shell-quoting trap and a wrong recipient/payload up front.
scripts/agent-dispatch --target-id codex-<project>-dev --dry-run-envelope "line one\nline two"
```

`scripts/codex-dispatch` is a thin deprecation wrapper around `agent-dispatch` — it prints a
stderr deprecation notice and `exec`s the canonical binary, so existing callers keep working
unchanged but get nudged. New callers should target `agent-dispatch` directly; the codex-named
alias will be removed in a future release.

### Common failure shapes

<!-- fragment:failure-shapes begin -->
| Error / Symptom | Likely cause | Fix |
|---|---|---|
| `[bridge-error] sender-rejected ...` | `FROM_AGENT_ID` is not in the target bridge's trusted-sender list | Set `FROM_AGENT_ID` to a value the bridge trusts, or have the operator add your ID |
| `envelope-invalid invalid-branch` | `BRANCH` is empty or `git branch --show-current` returned `""` | Hardcode `BRANCH=dev` or the intended branch in the dispatch invocation |
| `bridge busy with task <uuid>` | All engine-pool slots on the target are occupied | Wait, cancel, or check whether `BRIDGE_MAX_PARALLEL` is set lower than needed |
| Bridge starts but rejects every dispatch | The target bridge may have no sender policies configured | Set `AGENT_TRUSTED_SENDERS` in the env file or pass `--sender-policy` on the CLI |
| Dispatch exits immediately: "pass --run-id ID ... or --adhoc" | agent-dispatch hard-refuses un-labelled dispatches (since 2026-07-01) | Mint a run-id (dispatch-dev auto-defaults one) or pass --adhoc for a throwaway |
| `agent-dispatch` exits 124 | Timeout reached before a matching reply landed | Increase `--timeout` if the task is still running, or inspect bridge logs for a crash |
| Commit body shows literal `\n` characters | Caller composed the body with Bash double quotes | Use `$'...'`, a heredoc, or the brief-to-file pattern |
| `LLEN inbox` reads 0 while a task is running | Normal BLPOP behavior; the bridge consumes atomically | Use task status/result keys or bridge logs, not inbox length |
| `NOPERM No permissions to access a key` on a foreign inbox `LLEN`/`KEYS`/`SCAN` | You are on the **self-hosted bus** (per-identity ACLs); browse + foreign read-back are denied by design | Not a bug. Use the recipient's reply as the consumption signal, `GET`/`TTL` on a known `:status` for presence. See `docs/self-hosted-bus.md` |
| Panel `refused_reconcile` naming a seat whose vote you never saw fail | On the self-hosted bus, a missing **audit-emitter** grant NOPERMs the emit in the seat daemon log, not your cockpit | Grep that seat's daemon log for NOPERM at vote time; check its `ARB_MEMORY_REDIS_URL` user; recover via new run-id + `supersedes:` (`docs/self-hosted-bus.md`) |
| Bridge log shows `[reply-sent]` but dispatcher does not exit | Caller inbox may be polluted with stale `kind=notify` envelopes | Pull bridge code to a dispatcher that drops notifies and set `BRIDGE_NOTIFY_INBOX=0` |
| Dispatch to a Claude seat fails as unknown engine | A raw model id (e.g. `claude-opus-4-...`) was passed as `--engine` — engines are harness names, not model ids | Use `--engine agent-sdk` with `--target-id asdk-<project>-<workspace>-<model>` |
| `Could not connect to Redis ...: Can't assign requested address` mid-run, after the task-id printed | Ephemeral-port exhaustion. Each `agent-dispatch` spawns a fresh `redis-cli` per BLPOP poll; a wide fan-out held open for tens of minutes exhausts local ports | Stagger to 2–3 concurrent dispatchers. **The reply is lost irrecoverably** — the task ran, but its result key is gone before you can read it, so never fan out a benchmark un-staggered |
| Reply gate returns `dirty_uncommitted` listing files the task never touched | The orchestrator edited the seat's workdir while the dispatch was in flight; the gate diffs against the state at task START | Never edit a seat's workdir mid-dispatch. Note this is **silent for tasks that start after the edit** — they baseline the dirt and report `no_changes_clean`, so one contaminating edit fails only the runs already in flight |
| Later dispatches in a queued fan-out exit 124 while earlier ones succeed | Seats are `--max-parallel 1`; queued dispatchers spend their client `--timeout` waiting their turn, not working | Set `--timeout` to at least `queue_depth × turn_timeout`; keep `--turn-timeout` at the review ceiling |
| Seat dies at startup with `ValueError: invalid sender policy: <id>:trusted` | `--sender-policy` pairs are separated by `=`, not `:` | Pass `<id>=trusted`; valid values are `trusted\|human\|reject` (`Bridge.parse_sender_policies`) |
| `ModuleNotFoundError: No module named 'redis'` from `dispatch_authority`, and the seat log shows NOTHING | `agent-dispatch` resolved a system python without the venv, so it died before enqueueing — the seat looks deaf but never received anything | Put `$PWD/.venv/bin` on `PATH` for the dispatch. Note the asymmetry: `arb-memory-harness-publish` needs `ARB_MEMORY_REDIS_URL` **sourced**, the dispatch step needs it **unset** (`env -u`) |
| `arb-memory-harness-publish` → `invalid brief: missing ## Assumptions section`, or `items[N] must be an object` | The brief has no assumptions block, or its items are strings. `scripts/review-brief` does not emit the section at all | Add `## Assumptions` with a JSON fence whose `items` are objects: `{"statement","status":"assumed"\|"demonstrated","vantage"}`; `demonstrated` also needs `artefact_id` + positive int `version` matching the target's vantage (`tools/faba/faba_schema.py::validate_dispatch_brief`) |
| Verdict close returns `refused_reconcile` with `expected exactly 1 dispatch manifest, found 2; run un-auditable` | The roster manifest was emitted twice under one run-id (e.g. re-emitted after seats were replaced). Two rosters means no single answer to "who was on this panel", so no verdict can be proven complete | Emit the manifest **last**, once seat ids are final. To recover: mint a NEW run-id, emit exactly one manifest, re-emit every seat's vote from its **verbatim** fence, close with `supersedes: <refused-run-id>`. The refused run stays in Postgres as the scar — intended |
<!-- fragment:failure-shapes end -->

## Managed Redis/Valkey buses

The bridge defaults to plain Redis on localhost (no TLS, no auth — localhost is the gate). For
cross-host orchestration — where dev / staging / prod hosts each run their own bridge but share a
single registry so any host's claude can dispatch to any other host's codex — you point all
bridges at one managed bus (DigitalOcean Valkey, AWS ElastiCache, Upstash, etc.) and set the
connection env vars:

```bash
AGENT_REDIS_HOST=<managed-bus-host>     # e.g. a managed Valkey endpoint
AGENT_REDIS_PORT=25061                  # managed clusters use non-default ports
AGENT_REDIS_DB=12                       # most managed Redis keep multi-DB
AGENT_REDIS_PREFIX=agent_scratch:
AGENT_REDIS_TLS=1                       # appends --tls to every redis-cli
AGENT_REDIS_USER=default                # ACL user; default ships with the cluster
AGENT_REDIS_PASSWORD=<secret>           # exported as REDISCLI_AUTH (NOT `-a`)
                                        # so the secret never appears in `ps`
```

The Python bridge, the dispatcher (`agent-dispatch`), and the helper scripts
(`agent-bridge-ping`, `agent-inbox-watcher`) all pick these up automatically — no other code
changes per host. To reverse the migration, restore the 4 base `AGENT_REDIS_*` lines and
`systemctl --user restart` the bridge units; local Redis on the original port is untouched.

Worked example: a single managed Valkey cluster fronts every host's dispatcher. Each host
registers its bridge with a workspace-qualified agent_id (`codex-<project>-dev`,
`codex-<project>-staging`, etc.). A claude on the dev host can then target
`--target-id codex-<project>-staging` and the staging-host bridge picks it up via the shared
registry.

**Security notes:**
- The `--no-auth-warning` flag is added implicitly so logs aren't spammed.
- The password is passed via `REDISCLI_AUTH` env, never as `-a` arg — keeps it out of `ps -ef`
  and shell history.
- The bridge does not validate the cluster's TLS certificate beyond what `redis-cli --tls` does
  (the system trust store). For self-signed clusters you'll need `--cacert` plumbed in — not yet
  supported.

The self-hosted bus is a different animal: per-identity ACLs mean browse and foreign read-back
are denied by design. See [`../../docs/self-hosted-bus.md`](../../docs/self-hosted-bus.md).

## Message Flow

Requesters send:

```json
{
  "id": "uuid",
  "from": "claude-<project>-dev",
  "branch": "feat/some-branch",
  "to": "codex-<project>-dev",
  "kind": "request",
  "sent_at": "2026-04-26T19:00:00+01:00",
  "payload": {
    "task": "Run git status and summarize it."
  }
}
```

Control messages are also supported while a task is active:

```json
{"kind":"steer","payload":{"task_id":"uuid","message":"focus on the failing test only"}}
{"kind":"cancel","payload":{"task_id":"uuid"}}
```

The bridge writes verbose per-task events to a Redis Stream:

```text
agent_scratch:task:<request-id>:events
```

Example stream entry:

```text
XADD agent_scratch:task:<request-id>:events * \
  type model_text \
  task_id <request-id> \
  data '{"delta":"running tests..."}'
```

The bridge also maintains a compact status hash for consumers that should not read raw stream
output:

```text
agent_scratch:task:<request-id>:status
  task_id
  state
  phase
  last_summary
  updated_at
```

Final structured output is stored separately:

```text
agent_scratch:task:<request-id>:result
```

Long-form announcements are stored out-of-band so inbox messages stay short:

```text
agent_scratch:announcement:<agent-id>:<announcement-id>
```

Inbox notifications should reference these with `detail_key` instead of embedding long summaries.

It sends only concise milestone `notify` envelopes and the final `reply` to:

```text
agent_scratch:agent:<requester>:inbox
```

## Sender Policy

Defaults:

```text
claude-<project>-dev=trusted
claude-<project>-staging=trusted
unknown=reject
```

Policy behavior:

```text
trusted -> per-turn approvalPolicy=never, thread sandbox=workspace-write
human   -> per-turn approvalPolicy=on-request, thread sandbox=workspace-write
reject  -> no engine turn; error reply
```

For the ACP engines, `trusted` maps to ACP session mode `yolo`; `human` maps to ACP session mode
`default` (`engines/generic_acp.py`).

Override with repeated flags — pairs are separated by `=`, not `:`:

```bash
PYTHONPATH=src python3 -m agent_redis_bridge \
  --sender-policy claude-<project>-dev=trusted \
  --sender-policy slack-<user>=human \
  --unknown-sender-policy reject
```

## Safety Knobs

```bash
--turn-timeout 3600
--max-message-bytes 131072
--max-task-events 500
--events-ttl 604800
--status-ttl 604800
--result-ttl 2592000
--announcement-ttl 2592000
--daily-request-limit 0
--daily-turn-seconds-limit 0
--usage-scope codex-<project>-dev-shared
--codex-bypass-approvals-and-sandbox
--approval-policy never
--sandbox workspace-write
--max-parallel 1                   # see ../../docs/bridge-parallelism.md
```

`--max-parallel` (or `BRIDGE_MAX_PARALLEL`) caps the number of concurrent turns per bridge
instance. Default `1` matches pre-parallelism behaviour; each extra slot spawns its own engine
CLI process on demand. See [`../../docs/bridge-parallelism.md`](../../docs/bridge-parallelism.md)
for the full design + operational notes.

`--approval-policy` and `--sandbox` are initial thread defaults. Per-request sender policy
overrides approval behavior for each turn; the sandbox remains the thread sandbox.

Daily usage guard defaults are disabled with `0`. When enabled, the bridge tracks request count
and wall-clock turn seconds in Redis under:

```text
agent_scratch:usage:<agent-id>:YYYYMMDD:requests
agent_scratch:usage:<agent-id>:YYYYMMDD:turn_seconds
```

When `--usage-scope` is set, the scope replaces the agent ID in those keys. This lets multiple
role-bearing instances share one account-level budget:

```text
agent_scratch:usage:codex-<project>-dev-shared:YYYYMMDD:requests
agent_scratch:usage:codex-<project>-dev-shared:YYYYMMDD:turn_seconds
```

TTL defaults:

```text
task:<id>:events  7 days
task:<id>:status  7 days
task:<id>:result  30 days
announcement:*    30 days
agent inboxes     no TTL
heartbeats        60 seconds
```

The intended consumption pattern is:

```text
Redis stream = observability
Claude inbox = decision points
Git diff/tests = truth
```

## Sister tool: `candid`

> **DEPRECATED (2026-06-30).** `candid`'s reason to exist — "the multi-minute floor the bridge
> path imposes" — was a bug, not an inherent cost: per-token Redis writes throttling codex over
> the managed-bus TLS RTT, fixed in `9da7761` the same afternoon `candid` was added (`ada513f`, 6
> minutes later). The bridge prose path is now fast, so route a quick adversarial second opinion
> through a normal codex dispatch. The tool is kept (not deleted) so existing `~/bin/candid`
> symlinks keep working; it won't be extended.

`tools/candid` is a **standalone CLI**, NOT a bridge feature. It wraps `codex exec` directly (no
bridge dependency) for sub-30s adversarial-critique dispatches during ideation — the workflow
that complements the bridge rather than competing with it. The bridge is for tool-using
implementation work; `candid` is for fast prose-only second opinions.

Install:

```bash
ln -s <bridge-clone>/tools/candid ~/bin/candid
# or copy if you can't symlink
cp <bridge-clone>/tools/candid ~/bin/candid && chmod +x ~/bin/candid
```

Usage:

```bash
echo "is this design wrong?" | candid                    # default adversarial mode
candid --mode code "review this approach"
git diff | candid --mode code                            # ad-hoc code review
cat plan.md | candid --mode plan                         # attack an implementation plan
candid --mode product "Is this service credible?"
```

Four modes (`default` / `code` / `product` / `plan`), `--effort` and `--model` overrides for
reasoning tuning. Reads stdin if no positional prompt is given. Same `~/bin/candid` install model
whether or not the bridge is running — fully standalone.

## Tests

The package has hard runtime dependencies (`redis`, `jsonschema`, `pynacl`, `psycopg[binary]`)
plus per-subsystem extras, and the suite needs `pytest`, `anyio`, and `playwright` on top — see
`pyproject.toml`. The runner is pytest (config in `[tool.pytest.ini_options]`), not unittest.

Create the venv **at `.venv` in the checkout root**, not elsewhere: some scripts (e.g.
`scripts/arb-orch-panel`) re-exec into `<checkout>/.venv/bin/python` when imported from any other
interpreter, which kills a pytest run silently at collection (exit 2, zero output), and the
bridge's worktree tooling mirrors that same `.venv` path.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[agent-sdk,arb-memory,arb-files,arb-email,visibility,bench]' pytest anyio playwright defusedxml
.venv/bin/python -m pytest        # main suite (tests/); live_bakeoff deselected by default
python3 -m compileall -q src tests
```

Verified on a fresh venv 2026-07-31: 3800 tests collect with zero errors under this recipe.
Database-backed subsets skip without their DSNs; run those through the non-skippable gates
`scripts/graph-sql-gate` (`ARB_MEMORY_DSN`) and `scripts/arb-messages-gate`
(`ARB_MESSAGES_TEST_DSN`), which refuse to run rather than skip-green. Suites under `tools/*`
are deliberately excluded from bare `pytest` — run them from their own directories.

`scripts/suite-gate` is the whole-suite form of that contract: it pins the passed count,
asserts the skip SET against `scripts/suite-gate-skip-allowlist.txt` (a new skip fails the
gate rather than blending into the count), and refuses unusable environments outright.

### Local host setup beyond the venv

Four host-side facts the DB-backed and browser-backed tests need. Miss them and a healthy
checkout reports failures that read like code defects — which is exactly what happened: six
were carried for weeks as "environmental", and one of those six turned out to be a real stale
test double hiding inside the label.

```bash
export ARB_MEMORY_DSN="postgresql://localhost:5432/arb_memory_test"
export ARB_MEMORY_MCP_DSN="postgresql://arbmem_mcp@localhost:5432/arb_memory_test"
psql "$ARB_MEMORY_DSN" -v ON_ERROR_STOP=1 -q -f src/arb_memory/schema.sql   # into public
.venv/bin/python -m playwright install chromium-headless-shell
```

1. **`ARB_MEMORY_DSN` in host form.** The socket form silently breaks tests that derive a
   connection string from it.
2. **`ARB_MEMORY_MCP_DSN` set explicitly.** The deny-proof tests reconnect as the read-only
   role, deriving that DSN by rewriting `arb_memory:` → `arbmem_mcp:` in `ARB_MEMORY_DSN`
   (`tests/arb_memory/test_eval_grants.py:64`). A DSN with no userinfo makes that rewrite a
   no-op, and the connection falls back to your OS user — under which "access denied" proves
   nothing. The tests assert `current_user` and so fail loudly rather than passing vacuously.
   The role needs `LOGIN`; under local trust auth it needs no password.
3. **`schema.sql` applied to `public`.** The visibility app's own connections resolve
   unqualified tables (`transcript_io` and friends) from `public`, not from the fixtures'
   per-test scratch schemas. This mirrors every real deployment and is what CI's "Prepare
   database" step does. Every `CREATE TABLE` is `IF NOT EXISTS`, so re-running is safe.
4. **The Playwright browser pinned by the installed library version.** Playwright resolves an
   exact build revision per version (1.62.0 → `chromium_headless_shell-1234`), so browsers
   cached by other projects on the same machine do not satisfy it.

### Pre-push gate (opt-in)

GitHub Actions has been off since `295b97aa` (2026-07-10, *"solo tool; re-enable later"*) --
the workflow is parked at `.github/workflows/ci.yml.disabled`. `.githooks/pre-push` is the
deliberate local substitute, chosen over re-enabling CI on 2026-08-17: it runs
`scripts/suite-gate` before anything lands on `dev` or `main`.

```bash
git config core.hooksPath .githooks
```

**Status: deliberately NOT installed on any host as of 2026-08-17**, and the reason is a
measurement rather than a preference. The gate costs ~530s, and profiling says that cannot be
brought down by choosing fewer tests: the slowest 40 account for ~226s, but the remaining ~304s
is spread across 4,896 tests averaging ~62ms each. Deselecting the seven worst files outright
still leaves ~5.7 minutes — a wait long enough that `--no-verify` becomes habit, and a gate
routinely bypassed is worse than no gate because it reads as protection nobody has.

Crossing into tolerable territory needs parallelism, not selection, and the suite is not
parallel-ready: `test_fifo_order` binds a fixed port 6390, the `redis_bus` fixture defaults to a
shared db 15, and the `agy_tmux` tests drive real tmux sessions, so `pytest-xdist -n auto` would
collide until each is made per-worker. (`arb_memory` is already safe — it works in per-test UUID
schemas.) Do that work first if a blocking gate is wanted; the hook is kept here as the design of
record, inert until someone runs the line above.

Meanwhile `./scripts/suite-gate` remains the deliberate pre-merge check, run by choice.

The prerequisites are the four above — the hook refuses to run without the DSNs rather than
gating on a suite that quietly skips its database-backed half. Measured on 2026-08-17: with the
DSNs set the suite is 4936 passed / 62 skipped; without them, 4345 passed / 653 skipped. All 591
tests that stop running are on the skip allowlist and 4345 still clears the floor, so `suite-gate`
alone cannot tell a configured host from an unconfigured one. That is by design — the floor is
pinned to a no-DSN baseline so the gate stays runnable on hosts with no database — which is
exactly why the requirement lives in the hook instead.

Its limits, all deliberate and worth knowing before relying on it:

- **`dev` and `main` only.** Worktrees share the common `.git` directory (`extensions.worktreeConfig`
  is unset here, so `core.hooksPath` is repo-wide), meaning the hook fires for every worktree on the
  host — including dispatched-seat worktrees that push feature branches unattended. Gating those on
  an eight-minute suite would break dispatch for no gain.
- **This host only.** A seat pushing from another machine is not covered. That is the standing cost
  of a local gate over CI, and the reason to revisit CI if work spreads across hosts.
- **Fail-open for older trees.** `core.hooksPath` is relative, so a worktree checked out before this
  commit has no `.githooks/pre-push` and pushes ungated.
- **`git push --no-verify` is the only bypass**, on purpose. `suite-gate` rejects env overrides as
  "an evasion channel"; a second, quieter hatch in the hook would undo that reasoning one layer up.

## See also

- [`../../SPEC.md`](../../SPEC.md) — envelope and protocol specification.
- [`engines/README.md`](engines/README.md) — the adapters, their support tiers, and how to add one.
- [`../../skills/using-agent-bridge/SKILL.md`](../../skills/using-agent-bridge/SKILL.md) — the
  orchestrator-facing operational guide (dispatch, monitoring, panels).
- [`../../docs/bridge-parallelism.md`](../../docs/bridge-parallelism.md) — engine pool design,
  `--max-parallel`, control envelope semantics.
- [`../../docs/self-hosted-bus.md`](../../docs/self-hosted-bus.md) — per-identity ACLs and the
  `NOPERM`-by-design read model.
- `scripts/agent-inbox-watcher` — reference inbox listener.

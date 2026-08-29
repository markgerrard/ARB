# Qwen pi worker seats — routing & gate

> **Status note (2026-07-06):** the default worker engine is now `pi-sdk`; examples
> that use `--engine pi-rpc` remain valid for pi-rpc fallback seats. The forge host
> also runs pi as a **warm orchestrator** now; see
> [`pi-orchestrator-operating-guide.md`](pi-orchestrator-operating-guide.md).

Two pi engines run on this host as systemd user services. Both reach the
Earendil `pi` CLI, billed via OpenRouter. The default engine for new
dispatches is **pi-sdk**; **pi-rpc** stays mounted as a fallback. Default
workdir is project-bound per-seat (per-dispatch worktree overrides as usual).

| Engine | Seat (role) | agent-id (`--target-id`) | Model | Tools | Context | Input / Output $/Mtok |
|---|---|---|---|---|---|---|
| **pi-sdk (default)** | worker | `pi-sdk-<project>-dev-qcn-w` | `qwen/qwen3-coder-next` | full | 262K | $0.11 / $0.80 |
| **pi-sdk (default)** | reviewer | `pi-sdk-project-b-dev-qcn` | `qwen/qwen3-coder-next` | read-only | 262K | $0.11 / $0.80 |
| pi-rpc (fallback) | worker | `pi-<project>-dev-qcn-worker` | `qwen/qwen3-coder-next` | full | 262K | $0.11 / $0.80 |
| pi-rpc (fallback) | reviewer | `pi-project-b-dev-qwen3codernext` | `qwen/qwen3-coder-next` | read-only | 262K | $0.11 / $0.80 |
| pi-rpc (off-ladder) | fallback-for-Codex-outage | `pi-project-b-dev-qwen37max` | `qwen/qwen3.7-max` | read-only | 1M | $1.25 / $3.75 |

All review seats carry `BRIDGE_PI_TOOLS=read,grep,find,ls` and accept
non-trusted turns. Worker seats omit that env so pi runs with full tools
(read/grep/find/ls/bash/edit/write) and the bridge refuses non-trusted turns
at the policy guard — only callers in `AGENT_TRUSTED_SENDERS` get through.

## pi-sdk vs pi-rpc seats

Both engines drive the same qwen3-coder-next model; the difference is **how
pi is driven**. pi-rpc wraps `pi --mode rpc` (the CLI's NDJSON-over-stdio
mode); pi-sdk wraps `@earendil-works/pi-coding-agent`'s TypeScript SDK via a
long-lived Node host at `tools/pi-sdk-host/host.mjs`. Both seats are
separately routable on the bus — `pi-sdk-` prefix is a distinct tool name
in `ENGINE_TO_TOOL` so derived agent-ids never collide.

**Why pi-sdk is the default** (decided 2026-06-07 after 10/10 gate parity):

- Canonical `finalText` harvested directly from `agent_end.messages` rather
  than accumulated `text_delta` chunks plus a `get_last_assistant_text`
  fallback. Survives text→tool→text message shapes that pi-rpc has to
  reconstruct from delta streams.
- Definitive `stopReason: "stop"|"length"|"toolUse"|"error"|"aborted"`
  field on every `turn/completed`, rather than string-matching exception
  names to detect abort vs model error.
- Single tool-event shape (`tool_execution_*` only); no camelCase /
  snake_case dedup logic. ~280 LOC of accumulated quirk-handling that
  pi-rpc needs structurally disappears.
- Role profile composes via `DefaultResourceLoader.appendSystemPromptOverride`
  rather than a `--append-system-prompt` flag string.

**Why pi-rpc stays mounted** as a fallback:

- Battle-cooled wire: NDJSON-on-stdout is grep-able / replayable directly
  from bridge logs without instrumentation.
- Useful if `host.mjs` or the pi SDK breaks across a version upgrade.

**Debug parity**: pi-sdk seats can set `BRIDGE_PI_SDK_EVENT_LOG=<path>` in
their per-seat env file to dump one NDJSON line per raw `AgentSessionEvent`
(including `agent_end`'s full `messages` array with usage / cost / stopReason)
— strictly more detail than pi-rpc's NDJSON exposes. Off by default; turn it
on for any seat you need a post-mortem trail on. Sample env-file line:

```dotenv
BRIDGE_PI_SDK_EVENT_LOG=/tmp/pi-sdk-events-%i.ndjson
```

(systemd `%i` expands to the instance name, so each seat gets its own log.)

## Routing decision (promoted 2026-06-06 — bounded implementation only)

**Default worker for bounded implementation tasks**: `qcn-worker`. The promotion
is **deliberately narrow**. The 2026-06-06 gate proves one thing strongly:

> qwen3-coder-next is excellent at small, explicit, bounded implementation
> tasks where the brief is concrete and the expected change shape is clear.

It does **not** prove the model should handle architectural judgement, auth /
billing / permissions, money flows, shared-DB migrations, ambiguous refactors,
or anything where the model has to discover the design rather than execute it.

The full routing rules — implementor ladder (`qwen3-coder-next → Composer 2.5
→ Codex GPT-5.5`), reviewer ladder, and **hard escalation triggers** — live in
[`implementor-routing.md`](implementor-routing.md). Refer to that doc for which
seat to use; this doc only covers the qwen-side seat infrastructure.

Quick summary: escalate **out of** `qcn-worker` to **Composer 2.5** or
**Codex GPT-5.5** (per `implementor-routing.md`) when any of these applies:

- Task crosses multiple files in a non-mechanical way (refactor, not edit).
- Model must infer architecture rather than execute a stated shape.
- Auth, billing, permissions, production data, migrations on shared DBs, or
  destructive ops are involved.
- Brief + worktree context approaches the 262K limit of coder-next.
- Previous worker fails review or produces dirty/partial output.
- Reviewer flags "technically works but not the requested shape".

`qwen37max` is **NOT** the next escalation step. It's an off-ladder fallback
used **only** when the Codex GPT bridge is unavailable for a task that would
otherwise route to Codex — see `implementor-routing.md`. If Codex is healthy,
skip qwen37max entirely.

The middle Qwen tier — `qwen/qwen3-coder` (480B/35B-active) — is **not stood
up**. On the synthetic probe it failed 4/88 with consistent silent input
stripping, unsolicited rule additions, and exact-error-format drift.

## Gate result — 2026-06-06 (real Laravel, dev.project-f)

10/10 PASS. Spec-stops (2) correctly justified, in-place edits (5) at exact
signatures/expressions/placement, new-file scaffolds (3) matching anonymous-
class / test conventions, awkward-spec trap (task 10) resisted with no
`is_null` / `trashed()` / extra-clause drift. Full test suite stayed green in
every worktree (84/84, or 85/85 where a test was added). Independent cold-Opus
review concurred (`/tmp/qwen-gate/review-verdict.md` at gate run time).

A second real-task validation (brain repo, 2026-06-06 same day): 13 commits
shipped to `origin/main` via 6 batches, including 5 head-to-head bake-offs
against Composer 2.5. qcn was 1.3–1.9× faster on every non-tied bake-off and
produced byte-identical or functionally-equivalent diffs on 4 of 5. The one
spec-adherence ding went to qcn (a redundant lazy import on the `q5 deep
/health` task). See [`implementor-routing.md`](implementor-routing.md)
"Operational evidence" for the wall-clock table.

Total OpenRouter spend for the gate: **$0.049**. Total wall clock for the 10
dispatches: **2m 32s** at 3-concurrent batching.

Gate criteria (the bar coder-next cleared) — kept here for future repeats:

- Expected artifact present.
- No unrelated files changed.
- Test suite green (or "no regression in pass count" if baseline is red).
- No "helpful" spec drift.
- No test weakening (deleted assertions, skipped tests, loosened expectations).
- Clean git state, or a valid commit produced by the orchestrator-commit path
  (see [`dispatching-implementation-briefs.md`](dispatching-implementation-briefs.md)
  for the brief-preamble convention that makes this work in worktrees).
- Independent reviewer (cold-Opus subagent or peer model) approves.

**Result rule:** if a re-gate passes ≥ 8/10 with no harness incidents,
coder-next stays default. If < 8/10, treat the failure pattern as the next
investigation — do not silently re-promote max.

## How to dispatch to a seat

From a Claude session (or anything that speaks the bridge envelope), use
`agent-dispatch` with `--engine pi-rpc` and the seat's agent-id. The shared
project-b env file is what tells dispatch which Redis bus to use; the
`FROM_AGENT_ID` is your sender identity for the sender-policy check.

```bash
cd /home/<user>/release4.project-b.example.com  # or any workdir
AGENT_ENV_FILE=/home/<user>/release4.project-b.example.com/.env.bridge \
FROM_AGENT_ID=claude-project-b-dev \
/home/<user>/AgentRedisBridge/scripts/agent-dispatch \
  --engine pi-rpc \
  --target-id pi-project-b-dev-qwen3codernext \
  --timeout 600 \
  --run-id "qwen-dispatch-$(date -u +%Y%m%dT%H%M%SZ)" \
  'Implementation brief here. Be specific about expected artifacts and constraints.'
```

For an isolated worktree dispatch (the normal case for implementation work),
add `--worktree <name> --worktree-base main`. See `scripts/agent-dispatch -h`
for the full flag set.

Swap `--target-id` to `pi-project-b-dev-qwen37max` when an escalation
trigger from the routing table applies. The brief and rest of the invocation
stay identical — the bridge handles model selection per seat.

## How to verify a seat is alive

```bash
# Service status
systemctl --user status agent-redis-bridge@pi-rpc-dev-qwen3codernext.service --no-pager

# Bridge registration log line ("online at …")
tail -5 ~/.local/state/agent-bridge-pi-rpc-dev-qwen3codernext.log

# End-to-end ping (counts toward OpenRouter spend — sub-cent)
agent-dispatch --engine pi-rpc --target-id pi-project-b-dev-qwen3codernext \
  --timeout 30 --run-id "qwen-ping-$(date -u +%Y%m%dT%H%M%SZ)" 'reply with exactly PONG and nothing else'
```

Identity-introspection prompts ("what model are you") are unreliable — both
qwen seats hallucinate (one says "Anthropic", the other says "Earendil Works"
picking up pi's harness branding). Confirm correct routing via the OpenRouter
credits delta instead — see the gotcha section below.

## How to add a new model seat

The wrapper recognises any instance named `pi-rpc-<workspace>-<role>` and
forwards `AGENT_MODEL` from the env file into `--model`. Adding a new seat is
four files / commands; no code change needed.

1. **Env file** at `run/pi-rpc-<role>.env` (mode 0600 — contains an API key):

   ```dotenv
   OPENROUTER_API_KEY=<key>
   AGENT_MODEL=openrouter/<provider>/<model>
   BRIDGE_PI_TOOLS=read,grep,find,ls    # review-only; omit for full-tools
   ```

   Use a different provider (Anthropic, OpenAI, Google, Groq, etc.) by
   swapping the env var name (`pi --help` lists them) and the `AGENT_MODEL`
   prefix. `run/` is gitignored.

2. **Systemd drop-in** at
   `~/.config/systemd/user/agent-redis-bridge@pi-rpc-dev-<role>.service.d/override.conf`:

   ```ini
   [Service]
   EnvironmentFile=/home/<user>/AgentRedisBridge/run/pi-rpc-<role>.env
   ```

   This layers on top of the templated service's shared project-b env file
   (which provides Redis transport + `AGENT_PROJECT`/`AGENT_WORKSPACE`/
   `AGENT_TRUSTED_SENDERS`). The new env file only overrides the
   pi/openrouter-specific keys, so existing seats are unaffected.

3. **Reload + enable + start**:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now agent-redis-bridge@pi-rpc-dev-<role>.service
   ```

4. **Smoke test**: dispatch a one-line `PONG` task to
   `pi-project-b-dev-<role>` and confirm the OpenRouter credits delta
   (≈15s propagation lag — don't trust the first sample).

The new seat's agent-id will be `pi-project-b-dev-<role>` because the bridge
derives it as `<tool>-<project>-<workspace>-<role>` and inherits `project` from
the shared env file. If you want a different project binding, set
`AGENT_PROJECT=<name>` in the per-seat env file from step 1.

## How to disable a seat

```bash
systemctl --user disable --now agent-redis-bridge@pi-rpc-dev-<role>.service
```

No code rollback needed — the templated service stays, the per-seat drop-in +
env file can be left in place for a quick re-enable, or removed if the seat is
gone for good.

## Gotcha: confirming routing via OpenRouter delta

Both qwen seats can identify themselves wrongly when asked. Always verify
routing via the OpenRouter usage endpoint, not via the model's self-report:

```bash
curl -sS https://openrouter.ai/api/v1/credits \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  | jq -r .data.total_usage
# … dispatch one PONG task …
sleep 15   # endpoint has ~15s propagation lag
# … re-query and compare. Sub-cent delta means the dispatch hit OpenRouter.
```

## Infrastructure pointers (where things live)

- Wrapper: `scripts/agent-redis-bridge-systemd` (recognises `pi-rpc` prefix;
  forwards `AGENT_MODEL` to `--model`).
- Templated service: `~/.config/systemd/user/agent-redis-bridge@.service`
  (shared by codex / agy / grok / cursor / pi-rpc instances).
- Per-seat env files: `run/pi-rpc-<role>.env` (0600; gitignored).
- Per-seat drop-ins: `~/.config/systemd/user/agent-redis-bridge@pi-rpc-dev-<role>.service.d/`.
- Shell-rc: `~/.bashrc` exports `OPENROUTER_API_KEY` so interactive
  `pi --provider openrouter …` also works without going through the bridge.

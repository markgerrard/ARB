# dsh-acp engine — DeepSeek Harness as an ARB bridge seat

**Status:** design, approved by Mark 2026-08-17 (scope, permission posture, worktree).
**Scope:** minimum viable seat — "kick the tyres". Prove dsh can be dispatched to
through the bridge and answer a turn. Audit/vote wiring, worktree-isolated dispatch
and launchd permanence are deliberately deferred (see § Out of scope).

## Why this exists

DeepSeek Harness (`dsh`) is DeepSeek AI's open-source agent harness
(`deepseek-ai/deepseek-harness`, forked privately). It is a
plugin-composed runtime: a `cordis.yml` names the plugins, and different
compositions expose different transports. We want a dsh seat on ARB so DeepSeek
V4 Pro can be dispatched to like any other bridge engine.

The model route is Mark's existing **opencode** credential. `opencode-go` is an
OpenAI-compatible gateway (`https://opencode.ai/zen/go/v1`, driver
`@ai-sdk/openai-compatible` per opencode's own provider catalogue) that serves
`deepseek-v4-pro` and `deepseek-v4-flash`. dsh's DeepSeek adapter takes
`DEEPSEEK_BASE_URL` as, in its own words, the "OpenAI-compatible host endpoint"
(`examples/jsonrpc-agent/README.md` § Runtime environment), so the gateway drops
straight in. No new credential is minted.

## The fork: two runtimes, one core

dsh ships two transports over the same agent core:

| Runtime | Transport | Who already speaks it |
|---|---|---|
| `dsh-jsonrpc-demo` | bespoke newline-delimited JSON-RPC 2.0 | `staff-agent-bridge/internal/dsh` (Go, in production) |
| `dsh-acp-demo` | **standard ACP** — `@agentclientprotocol/sdk` 0.25.1 (`packages/acp/acp/package.json`) | ARB's nine existing ACP engines, via `engines/_acp.py` + `gemini_acp.py` |

**We take the ACP door.** ARB already owns the ACP stack, so the adapter is a
subclass rather than a new transport; the thinnest existing example,
`engines/opencode_acp.py`, is 78 lines. The bespoke wire would mean writing
transport, session-resume and event-mapping from scratch in Python to reach the
same place.

The secondary argument is durability. dsh's README states it is in developer
preview and that "THERE WILL BE COMPATIBILITY-BREAKING CHANGES". A break in a
standard both DeepSeek and nine other vendors implement is a protocol regression
others hit too; a break in dsh's private wire is ours alone to discover.

## Evidence: what dsh's ACP surface actually does

Probed 2026-08-17 by sending the exact frames `GeminiAcpEngine` sends, against
`packages/examples/acp-demo/lib/bin.js --config examples/acp-agent/cordis.yml`,
routed at the opencode gateway. Verbatim results:

| Frame (ARB source) | dsh response |
|---|---|
| `initialize{protocolVersion:1,clientInfo,clientCapabilities}` (`gemini_acp.py:88`) | ok — `agentInfo: deepseek-harness-acp 0.0.1`, `protocolVersion: 1`, `authMethods: []` |
| `session/new{cwd,mcpServers}` (`gemini_acp.py:104`) | ok — returns `sessionId` |
| `session/set_model{sessionId,modelId}` (`gemini_acp.py:111`) | **`-32601 Method not found`** |
| `session/set_mode{sessionId,modeId}` (`gemini_acp.py:226`) | **`-32601 Method not found`** |
| `session/prompt{sessionId,prompt:[{type,text}]}` (`gemini_acp.py:130`) | ok — `stopReason: end_turn`; one `agent_message_chunk` notification carrying the model's reply |

The prompt was a live turn: the model returned the requested token through the
opencode gateway. Since that key opens no other endpoint, a successful turn is
itself evidence the intended route was used.

`agentCapabilities` advertises only `promptCapabilities` (image/audio/embeddedContext
all false). No `sessionModes`, no model list.

### What the two `-32601`s mean

They are not defects; they are a different **locus of control**. ARB's ACP engines
assume model and permission posture are per-session and set over the wire. dsh
treats both as composition properties fixed when the process boots:

- **Model** is `acp-agent.config.model` in the cordis file
  (`examples/acp-agent/cordis.yml:54-55` — `provider: deepseek-official`,
  `model: deepseek-v4-pro`).
- **Permission posture** is `DSH_PERMISSION_MODE`, which selects both the sandbox
  policy mode and the approval plugin's policy (`cordis.yml:29` and `:44`:
  `danger-full-access` ⇒ approval `never`, otherwise `ask`).

Consequence: **a dsh seat's model and trust posture are properties of its plist,
not of the dispatch** — the same shape as a pi seat's model pin.

## Design

### Component 1 — `src/agent_redis_bridge/engines/dsh_acp.py`

Subclass of `TurnPolicyPermissionMixin + GeminiAcpEngine`, modelled on
`opencode_acp.py`. Three overrides, each justified by a probe result above:

1. `command_args()` → `[node, <DSH_ACP_BIN>, "--config", <DSH_ACP_CORDIS>]`.
   The base assumes `[self.command, "--acp"]`; dsh is a node script plus a config
   flag, so the whole argv is replaced.
2. `start_session()` → `session/new` only. The base's `set_model` call is dropped
   because dsh answers it `-32601`.
3. `set_session_mode_for_policy()` → documented no-op. The base sends
   `session/set_mode` before **every** turn (`gemini_acp.py:226`); left inherited,
   every dsh turn would die at the mode call before reaching the model.

`supports_thread_resume = False`. dsh may implement `session/load`, but it was not
probed, and declaring resume on an untested method is exactly the unverified claim
this design's evidence table exists to avoid.

The docstring records the `-32601` evidence and the date, so a future reader can
tell a deliberate omission from an oversight.

### Component 2 — an ARB-owned cordis config

`configs/dsh/acp-agent.cordis.yml`, ARB's own copy of the composition. We do
**not** edit the upstream clone: it is a fork we will pull from, and a local edit
there is a silent merge conflict waiting to happen.

The copy is the upstream file verbatim except for **two** changes:

1. the model reads `!!js process.env.DSH_ACP_MODEL ?? 'deepseek-v4-pro'`;
2. `persistenceRoot` also consults `DSH_SESSION_ROOT`, so session logs land
   outside the seat's working tree (see the persistence finding below).

Verbatim otherwise matters because the built bin only carries the plugins its
own composition names — an invented plugin set fails at boot.

*(An earlier revision of this section said "exactly one change" and the config's
own comment said "the only one". The second change was added during
implementation and the prose was not reconciled — caught by the review panel,
which is the same overclaiming-prose class as the other findings.)*

**Revised during implementation.** The approved design had the engine parse the
cordis file and fail loud on a model mismatch. That required parsing YAML
containing `!!js` tags, which PyYAML rejects. Reading the model from the
environment instead makes the seat's plist the single source of truth and reduces
the engine's job to comparing two strings. Detecting a mismatch is strictly worse
than making one unrepresentable.

Verified 2026-08-17: booting this composition with `DSH_ACP_MODEL=deepseek-v4-flash`
and asking the model to name itself returned `deepseek-v4-flash` — dsh's persona
template interpolates `{{model}}`, so the model itself reports what the
composition booted with.

#### The plugin-resolution constraint (discovered during implementation)

cordis names plugins as bare specifiers (`@deepseek-ai/dsh-hooks-codex`) and the
runtime resolves them with Node's ESM algorithm rooted at **the config file's
directory**, walking upward — not the bin's directory, not the cwd. In the
harness's pnpm workspace those packages are linked at
`<harness>/examples/node_modules` (the `examples` workspace package declares
them), which is why the upstream config resolves: it sits one level below.

ARB's copy sits in a Python repo with no `node_modules` anywhere above it, so it
died at boot with `ERR_MODULE_NOT_FOUND`. That reached the bridge as a 60-second
handshake timeout followed by a broken pipe — no usable diagnosis.

Two consequences, both implemented:

1. The seat setup symlinks `configs/dsh/node_modules` →
   `<harness>/examples/node_modules` (gitignored, host-local — the same class of
   materialisation as the venv mirror for worktrees).
2. `DshAcpEngine._assert_plugins_resolvable` walks up from the config's directory
   at construction and refuses with the fix instruction if no `node_modules` is
   reachable. An opaque timeout becomes a loud, actionable error.

### Component 3 — registration in `bridge.py`

- `dsh-acp` added to the engine choices.
- A `build_engine` branch returning `DshAcpEngine(cwd=..., model=args.model)`.
- Agent-id prefix map entry `"dsh-acp": "dsh"`, alongside `"opencode-acp": "opencode"`,
  so seats register as `dsh-<project>-<workspace>`.
- **Not** added to `resume_classes`, per `supports_thread_resume = False` above.

### Component 4 — the seat

`com.example.arbseat.dsh-bridge-dev.plist`, modelled on the opencode seat's plist
(same host, same key, same bus). `DEEPSEEK_API_KEY` lives in an env file rather
than inline in the plist.

## Error handling

- **Model disagreement fails loud at startup.** Because `--model` cannot reach the
  wire, a seat launched with a `--model` that disagrees with `DSH_ACP_MODEL` must
  refuse to start rather than silently serve a different model. Silent substitution
  would make every downstream claim about "which model reviewed this" false.
  Mirrors `opencode_acp.py`'s "loud failure rather than silent fallback".
- **Unresolvable plugins fail loud at startup** — see the plugin-resolution
  constraint above.
- **Runtime death** surfaces through the base's stderr drain; dsh writes
  diagnostics to stderr by design because stdout is the ACP wire.
- **Gateway errors** (bad key, unknown model) arrive as a failed turn, not a
  handshake failure — `initialize` and `session/new` both succeed without ever
  contacting the gateway, so a credential problem will not show up until the first
  prompt. Worth knowing when diagnosing: a seat that pings healthy proves nothing
  about the model route.
- **Permission requests.** Under `workspace-write` the approval policy is `ask`, so
  a tool-using turn issues a client-bound permission request. `TurnPolicyPermissionMixin`
  answers these; the probe confirmed none are issued for a tool-free turn.

## Testing

- Unit tests against a fake ACP server asserting that `set_model` and `set_mode`
  are **never sent** — these must fail if either override is deleted, which is what
  makes them worth having.
- A test asserting `command_args()` shape.
- A test asserting the loud failure on model/config mismatch.
- The live probe (`agent-dispatch` round trip returning a real reply) is the
  acceptance gate, per Mark's stated done-criterion.

### Live gate result (2026-08-17)

A dispatch from `claude-bridge-dev` to
`dsh-bridge-dev`. Reply verbatim:

> 1. **Model:** deepseek-v4-pro (named in my system prompt: "You are a coding
>    assistant powered by the deepseek-v4-pro model").
> 2. **Working directory:** `/Volumes/<workspace>/repos/ARB/.claude/worktrees/dsh-acp`
> 3. **Directory entries:** 25.
> 4. **First line of README.md:** `# Agent Redis Bridge`

`ok: true`, `completion.state: no_changes_clean`, `dirty_files: []`.

The four answers exercise four different layers, so the pass is not a single
undifferentiated "it worked": the model route resolved (1), the ACP session's
`cwd` reached the tools (2), bash executed under the sandbox policy (3), and the
filesystem tool read under it (4).

**The first attempt is the more informative run.** It returned all four answers
correctly and still came back `ok: false`, bounced by the completion gate as
`dirty_after_commit` — the runtime had written `.sessions/…/session.jsonl.zstd`
into the seat's working tree. That produced the persistence fix above. Two
mechanisms each behaving exactly as designed can still compose into a failure
neither owns, and only a real dispatch surfaces it: no unit test against a fake
ACP server would have caught a runtime writing files.

**Process note.** That first run also shows `head_before` ≠ `head_after`, because
the orchestrator committed to the seat's workdir while the dispatch was in
flight. The bounce was caused by the session file, not the commit, but the rule
("never edit a seat's workdir mid-dispatch") was broken and the evidence records
it. The re-run was done with a clean, untouched tree.

## Permission posture (decided)

`DSH_PERMISSION_MODE=workspace-write` — dsh's own default. bash and filesystem
tools are confined to **the runtime process's cwd**; wider access raises a
permission request the bridge answers. Chosen by Mark over `danger-full-access`.

**Correction (panel finding cold-Opus P1-5).** An earlier revision of this
section said "confined to the session cwd". That was false. The composition pins
`workspaceRoot` and the fs-sandbox `cwd` to `process.cwd()` — the node child's
working directory — while the ACP base spawns that child *without* a `cwd=`
argument, so it inherits the bridge daemon's cwd. The session's cwd is a separate
value sent in `session/new`. Two independently-set knobs (launchd
`WorkingDirectory` and `--workdir`) that merely coincided during the live gate,
which is precisely why the gate passed and proved nothing about the case where
they differ.

`DshAcpEngine._assert_sandbox_root_matches_session_cwd` now refuses to construct
a seat where they diverge, so the confinement claim is true by enforcement rather
than by coincidence of configuration.

Consequence to record: ARB's `trusted` vs `human` sender policy normally maps onto
session mode, and here it cannot. Both policies get the same sandboxed posture, and
sender policy governs only *who may dispatch at all*. A dsh seat is therefore not
equivalent to a trusted codex seat, and should not be described as one.

## Out of scope (deliberately deferred)

- Audit/vote fence wiring for panel participation.
- `--worktree` isolated dispatch and the venv mirror.
- Thread resume (`session/load`) — unprobed.
- The `readonly_gate.py` allowlist: like opencode, dsh has no tool-allowlist flag,
  so this seat cannot be certified read-only and stays out of that set.
- The bespoke JSON-RPC runtime. It remains a viable fallback, already proven by
  `staff-agent-bridge/internal/dsh`, if the ACP surface turns out too thin.

## Open risks

1. **dsh is a developer preview.** Compatibility breaks are promised by its own
   README. The ACP choice mitigates but does not remove this.
2. **Model pinning is per-process.** Switching models means a second seat or a
   restart, not a dispatch parameter.
3. **`agentCapabilities` is sparse.** dsh advertises no session modes and no model
   list, so nothing about the seat's model is discoverable at runtime — the plist
   is the only record of what a given seat actually is.

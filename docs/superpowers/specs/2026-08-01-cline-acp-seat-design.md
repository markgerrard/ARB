# Cline ACP seat — DeepSeek4 Flash (design)

Date: 2026-08-01. Author: warm orchestrator (Claude Fable 5), autonomous run authorized by Mark
("do this autonomously", 2026-08-01 morning). Status: implemented same-day; this doc records the
design and the probe evidence it rests on.

## Goal

Add `cline-acp` as a bridge engine and stand up one seat — `cline-bridge-dev` — pinned to
`deepseek/deepseek-v4-flash` through Mark's Cline subscription, joining the bridge-dev fleet as an
experimental (non-certifying) seat.

## Probe evidence (2026-08-01, cline CLI 3.0.48, captures in /tmp/cline-acp-probe.jsonl)

All wire-surface claims below were observed live, not assumed:

- `cline --acp` speaks ACP protocolVersion 1 over stdio; `initialize` → `session/new` →
  `session/prompt` round-trips; a shell tool call raised a standard
  `session/request_permission` with `allow_once`/`allow_always`/`reject_once` options and the
  turn ended `stopReason=end_turn`.
- `session/update` vocabulary observed: `agent_message_chunk`, `tool_call`, `tool_call_update`,
  `session_info_update` — a subset of Devin's, so Devin's normalizer transfers.
- `session/new` returns `modes` (`plan`/`act`, current `act`), `models.availableModels`
  (276 models, including `deepseek/deepseek-v4-flash`), and Devin-style `configOptions`
  (`provider`, `model` selects).
- **The ACP session does NOT inherit the CLI's sticky model config**: with `providers.json`
  set to `deepseek/deepseek-v4-flash` and `-m deepseek/deepseek-v4-flash` on argv, the session
  still booted `currentModelId=anthropic/claude-sonnet-4.6`. Argv `-m` is a no-op in ACP mode.
- `session/set_config_option {configId: "model"}` works and the response echoes the full
  `configOptions` with the new `currentValue` — a read-back verification hook.
- `session/set_mode` (`plan`/`act`) works.

## Approaches considered

1. **New `ClineAcpEngine` modeled on `DevinAcpEngine`** — CHOSEN. Cline's config surface
   (`configOptions` with a `model` select) is Devin's shape almost exactly; permission handling
   reuses the shared, panel-reviewed `_acp._select_allow_option`.
2. Generalize an ACP base class and refactor devin/cursor/cline onto it — rejected: cross-cutting
   refactor of reviewed engines is out of scope for a seat addition; the repo deliberately keeps
   engines separate with shared helpers in `_acp.py`.
3. Drive cline headless non-ACP (`cline --json <prompt>`) like agy-print — rejected: loses
   streaming, tool visibility, and permission mediation that ACP gives for free.

## Design

### Adapter: `engines/cline_acp.py` (`ClineAcpEngine`)

Devin-pattern stdio JSON-RPC client. Differences from Devin, each grounded in probe evidence:

- `command_args() = ["cline", "--acp"]`.
- **Model set is load-bearing and hard-fails.** Devin warns-and-continues when model selection
  fails; here the session default is a *different vendor's model* (`anthropic/claude-sonnet-4.6`),
  so fail-open means a "DeepSeek4 Flash seat" silently reviewing as Sonnet — misattributed
  findings, wrong billing, broken panel decorrelation assumptions. `start()` therefore raises
  `EngineError` if: no model was configured for the seat, the model cannot be resolved against
  `configOptions`/`availableModels`, `set_config_option` errors, or the response read-back
  `currentValue` ≠ target. A cline seat ALWAYS pins a model explicitly.
- **Policy mapping:** `trusted` → `session/set_mode act` + permission asks answered via
  `_select_allow_option`; `human` → `session/set_mode plan` + asks denied fail-closed
  (belt-and-braces: plan mode shouldn't mutate, and denies count against
  `BRIDGE_APPROVAL_DENY_BUDGET`, Devin/grok parity). Asks for a non-current session are denied
  regardless of policy (grok D3b parity).
- `retire_after_turn = True` — cline keeps sticky shared state under `~/.cline`; a fresh process
  per dispatch keeps seats deterministic. `supports_thread_resume = False`,
  `supports_continuation = False` (v1; `loadSession` exists upstream if we ever want resume).
- Event normalization: per-engine `normalize_session_update` with Devin's vocabulary (message
  chunks, thought chunks, tool_call/tool_call_update with diff-path enrichment,
  session_info_update, unknown-update passthrough). Thought chunks weren't observed in the probe
  (reasoning ran without them) but are in cline's upstream vocabulary; handling them is harmless.

### Registration surface

- `ENGINE_TO_TOOL["cline-acp"] = "cline"` (also feeds `--engine` argparse choices).
- `bridge.py`: import, `build_engine` branch (`ClineAcpEngine(cwd=cwd, model=args.model)`),
  `resume_classes` entry (resolves False via `supports_thread_resume`).
- `support_tiers.py`: `"cline-acp": EXPERIMENTAL` — test-enforced; new seats start
  non-certifying per the roster rules; calibration happens through live panel samples later.

### Seat: `cline-bridge-dev`

Launchd plist `com.example.arbseat.cline-bridge-dev.plist`, cloned from the devin seat shape:
fleet clone `/Users/<user>/<workspace>`, env file `envs/agent-redis-bridge-dev.env`,
`--engine cline-acp --project bridge --workspace dev --model deepseek/deepseek-v4-flash`,
same sender-policy trio (claude-bridge-dev, codex-bridge-dev-example, codex-arb-codex-dev-sol as
trusted), `--max-parallel 1`, logs to `~/Library/Logs/agent-bridge/cline-bridge-dev.log`.

Auth rides Mark's existing Cline subscription login (`~/.cline/data/settings/providers.json`,
refresh token present). Auth-decay failure shape: if the token dies, expect the grok-style
"pings pass, turns fail" ambiguity — check the seat log before re-authing interactively.

### Testing

`tests/test_cline_acp.py` patterned on `test_devin_acp.py`: command_args, start/handshake with
FakeProcess, model resolution (exact value, then name), **model-set hard-fail cases** (missing
model, unresolvable model, read-back mismatch — each asserts the specific `EngineError` message,
not a bare raise), policy→mode mapping, permission allow/deny paths incl. wrong-session deny and
deny-budget exhaustion, normalize_session_update vocabulary, turn lifecycle (end_turn ok,
cancelled not-ok, process-exit crash path). Plus the `support_tiers` entry keeps
`test_engine_support_tiers.py` green.

### Live gate (before the seat is declared usable)

1. `agent-bridge-ping` shows registry+heartbeat+consumer alive.
2. One real dispatch through the quartet path with a task requiring a shell command; verify from
   the SEAT LOG (`[turn-start]`/`[turn-tool]`/`[reply-sent]`) and the reply payload that (a) the
   turn executed a tool, (b) the reply landed. Reply prose alone is a claim, not evidence.
3. Model attribution: the hard-fail design means a running seat IS the evidence the model pin
   applied (start() would have killed the engine otherwise); the seat log's absence of
   model-set errors is the artifact.

## Live-run addendum (2026-08-01, standalone engine tester)

A real turn through `ClineAcpEngine` (handshake → model pin → act mode → permission allow →
shell tool → `end_turn`) passed. Two observations for future seat operators:

- **DeepSeek4 Flash hallucinates its identity as `claude-3-5-sonnet-20241022`** in reply prose.
  Ground truth from the cline session record (`~/.cline/data/sessions/<id>/<id>.json`):
  `model = "deepseek/deepseek-v4-flash"`, and usage cost (~$0.00028 for ~6k tokens) is
  flash-tier, ~50× below Sonnet pricing. Do not use self-report to check a seat's model;
  the engine's read-back plus the session record are the evidence.
- **cline's `session_info_update` is a bare `{updatedAt}` heartbeat** (no `title`); the
  normalizer drops it rather than emitting unknown-update noise.

## Live-gate record (2026-08-01, seat `cline-bridge-dev`)

Two full dispatches through the quartet path PASSED:
dispatcher exit 0, payload `ok=true`, seat log `[turn-end] ok` + `[reply-sent]`, completion gate
`no_changes_clean`, correct HEAD (`b33a38b6`) reported from inside the seat workdir. Findings:

- **~~Cline sanitizes tool-shell environments~~ — CORRECTED same day (see "Hub root cause"
  below).** The original canary evidence was real but the mechanism attribution was wrong:
  cline does not sanitize anything. Tool shells execute inside a user-global **hub daemon**
  that inherits its environment from whichever cline invocation first spawns it — the "missing"
  DSN was a stale hub born from a DSN-less shell. The bridge's hydration-readiness probe still
  cannot see this (it probes from the daemon's vantage) — that vantage-gap point stands.
- **The worker self-recovered — 2/2 — while the stale hub was up.** In both gate runs DeepSeek
  diagnosed the missing DSN, sourced `~/.arb-memory-local/readers.env`, retried, and hydrated
  (dev store missed, prod store hit). The seat runs `BRIDGE_TASK_REF_REQUIRED=1` (wave-1
  conformant).
- **Store-tier note:** `arb-memory-harness-publish` under the dev env file lands briefs in the
  PROD memory store while the seat's local-MCP tier defaults to dev — the worker had to try both.
  Pre-existing wiring, not cline-specific; flagged for reconciliation.
- **Restart invalidates receipts.** A seat restart changes `registration_generation`, and
  dispatch-authority correctly refuses receipts minted against the prior boot ("receipt
  registration_generation mismatch") — re-publish after any seat restart.
- `src/arb_memory/local_read_policy.py:12` error prose overclaims ("ARB_MEMORY_LOCAL_MCP is
  set…" is emitted unconditionally when the DSN is missing) — the worker independently flagged
  this too. P3, prose-claim defect class; not fixed in this slice.

## Hub root cause + deterministic-hydration fix (2026-08-01, same day)

Checked for a cline env-passthrough setting per Mark's instruction: none exists (README env
list, settings surface, and the `@cline/core` bundle all checked; the one filtered-looking
spawn is `withResolvedClineBuildEnv`, not a filter). The actual mechanism, proven by canary:

- One-shot and ACP cline sessions delegate tool execution to a user-global **hub daemon**
  (`~/.cline` state, port 25463) spawned on demand by the first cline invocation and inheriting
  THAT invocation's environment. Kill the hub and the canary reaches the tool shell; while a
  stale hub lives, no env change on the invoking process matters. This also means tool-shell
  env can silently belong to an arbitrary historical shell (the interloper we displaced carried
  an SSH session's Slack tokens — worth knowing as a general cline property).

**Fix shipped:** `com.example.cline-hub.plist` — a launchd-managed hub (`CLINE_RUN_AS_HUB_DAEMON=1`
+ observed daemon argv, `KeepAlive`, port 25463) whose environment pins `ARB_MEMORY_LOCAL_DSN`
(the same read-only reader credential every seat plist carries). KeepAlive means the managed hub
holds the port before any ad-hoc spawn can race it, making tool-shell env deterministic for the
seat AND for interactive cline use.

**Gate evidence:** the brief
forbade sourcing readers.env or touching the DSN; the worker hydrated first-try from the hub
env alone, reported DSN presence count 1 (value never printed), HEAD `b33a38b6`, completion
`no_changes_clean`, dispatcher exit 0. Hydration on this seat is now mechanism, not initiative.

Residual: a cline version upgrade may change the hub daemon argv/port — if the managed hub
fails after an upgrade, re-derive the argv from a running ad-hoc hub (`pgrep -fl
cline-hub-daemon`) and update the plist.

## Out of scope

- Roster/calibration doc updates (`arb-seat-roster.md`, panel composition) — the seat needs live
  samples before calibration prose exists; premature entries would be authorial assertion.
- Session resume (`loadSession`), image prompts, cline hooks/zen mode.
- Any certify-quorum change (owner-set; this seat is experimental adjunct at most for now).

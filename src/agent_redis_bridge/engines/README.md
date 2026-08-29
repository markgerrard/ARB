# Engine adapters

One `--engine` name, one adapter module, one seat family. The bridge daemon is engine-neutral:
`bridge.build_engine()` picks an adapter, the adapter drives somebody else's CLI, and the
daemon sees only normalized task events. Everything engine-specific lives in this directory.

Two lists elsewhere have to agree with what is here, and both are pinned by tests:
`ENGINE_TO_TOOL` in [`../bridge.py`](../bridge.py) (what the daemon can run, and the agent-id
prefix it derives) and the `case "$ENGINE" in` block in `scripts/agent-dispatch` (what can be
dispatched to). A seat that runs but cannot be dispatched to is the exact drift
`tests/test_dispatch_engine_parity.py` exists to catch — it happened, with `cline-acp`, and was
found four days later by a certification panel.

## The engines

Support tiers come from [`support_tiers.py`](support_tiers.py), which is the oracle — it is
asserted against both `ENGINE_TO_TOOL` and this directory's module list by
`tests/test_engine_support_tiers.py`, so an unclassified adapter is a red test rather than a
memory exercise.

| `--engine` | Agent-id prefix | Tier | Adapter | Transport |
|---|---|---|---|---|
| `codex` | `codex-` | **certifying** | [`codex.py`](codex.py) | Codex App Server (JSON-RPC over stdio) |
| `agy-print` | `agy-` | **certifying** | [`agy_print.py`](agy_print.py) | `agy --print`, transcript read back from SQLite |
| `pi-sdk` | `pi-sdk-` | **certifying** | [`pi_sdk.py`](pi_sdk.py) | Node host over JSON-RPC/stdio, modelled on the codex app-server wire |
| `agent-sdk` (alias `asdk`) | `asdk-` | experimental | [`agent_sdk.py`](agent_sdk.py) | Claude Code Agent SDK seats |
| `agy-tmux` | `agy-tmux-` | experimental | [`agy_tmux.py`](agy_tmux.py) | `agy` driven inside a real tmux session |
| `cline-acp` | `cline-` | experimental | [`cline_acp.py`](cline_acp.py) | ACP, via `DenyBudgetAcpEngine` |
| `cursor-acp` | `cursor-` | experimental | [`cursor_acp.py`](cursor_acp.py) | ACP, via `HealthReportingAcpEngine` |
| `devin-acp` | `devin-` | experimental | [`devin_acp.py`](devin_acp.py) | ACP, via `DenyBudgetAcpEngine` |
| `dsh-acp` | `dsh-` | experimental | [`dsh_acp.py`](dsh_acp.py) | ACP (`GenericAcpEngine` + permission mixin) |
| `grok-acp` | `grok-` | experimental | [`grok_acp.py`](grok_acp.py) | ACP, via `HealthReportingAcpEngine` |
| `kimi-code-acp` | `kimi-` | experimental | [`kimi_code_acp.py`](kimi_code_acp.py) | ACP (`GenericAcpEngine`) |
| `mini-agent-acp` | `minimax-` | experimental | [`mini_agent_acp.py`](mini_agent_acp.py) | ACP (`GenericAcpEngine`) |
| `omp-acp` | `omp-` | experimental | [`omp_acp.py`](omp_acp.py) | ACP (`GenericAcpEngine` + permission mixin) |
| `opencode-acp` | `opencode-` | experimental | [`opencode_acp.py`](opencode_acp.py) | ACP (`GenericAcpEngine` + permission mixin) |
| `openinterpreter` | `interpreter-` | experimental | [`openinterpreter.py`](openinterpreter.py) | Open Interpreter, with the scored tool-plane broker |
| `pi-rpc` | `pi-` | experimental | [`pi_rpc.py`](pi_rpc.py) | `pi --mode rpc` NDJSON |
| `gemini-acp` | `gemini-` | **retired** | [`gemini_acp.py`](gemini_acp.py) | dead — see below |

Tier meanings, from the module that declares them:

- **certifying** — backs seats eligible for certify quorums per
  [`../../../docs/agent-role-routing.md`](../../../docs/agent-role-routing.md) and the panel
  section of [`../../../skills/using-agent-bridge/SKILL.md`](../../../skills/using-agent-bridge/SKILL.md).
  It records the current routing-doc calibration; per-round panel composition stays owner-set.
- **experimental** — live and dispatchable, but non-certifying: adjunct reviewers whose severity
  labels are advisory, explicitly experimental implementors, newly landed adapters with no
  deny-proof behind them yet, and harness variants.
- **retired** — the wrapped CLI is dead. `build_engine` refuses to construct the adapter, and the
  operator entry points reject it by name. Deprecation is a **reason, not an exemption**: the
  `agent-dispatch` case entry must still exist so the caller gets the explanation rather than
  `unknown engine`.

> **RETIRED (2026-07-03): `gemini-acp`.** Google deprecated the `gemini` CLI and it stopped
> working, so the gemini-acp engine no longer runs. It is removed from the launcher's
> known-engines list, and `agent-dispatch` / `agent-bridge-ping` reject `--engine gemini-acp`
> with a deprecation error. The module is retained only for its ACP-protocol unit tests, and as
> a thin identity shim over `generic_acp.py` (see below). Use codex / grok-acp / agy-print /
> pi-sdk instead.

Prefixes are not cosmetic: an omitted `--target-id` is *derived* from
`<tool-prefix>-<project>-<workspace>`, so a wrong prefix addresses a seat nobody registered.
That is why `pi-sdk` takes `pi-sdk-` rather than `pi-` (pi-sdk and pi-rpc seats coexist on one
bus) and why oh-my-pi takes `omp-` despite being a pi fork.

## The shared ACP base

Ten of the seventeen adapters speak the [Agent Client Protocol](https://agentclientprotocol.com)
over stdio. That client was forked five ways before the collapse onto a shared base — cline 697,
devin 667, cursor 591, gemini 466, grok 865 lines, with a 0.85 difflib line ratio between cline
and devin. The layering that replaced it, from [`_acp_base.py`](_acp_base.py):

- **`AcpEngineBase`** — the transport plus the common prompt loop: process lifecycle, JSON-RPC id
  allocation, the stdout reader thread, request/notify, client-message dispatch.
- **`HealthReportingAcpEngine`** — adds `is_healthy`. Deliberately *not* on the transport base:
  `engine_pool.py` consults `is_healthy` only when the engine defines it, so hoisting it would
  silently opt the generic-acp family into pool-side health quarantine — a behaviour change, not
  a refactor.
- **`DenyBudgetAcpEngine`** — the turn-scoped approval deny budget and the policy-threaded
  permission responder shared by `devin-acp` and `cline-acp`.

Permission *decisions* stay in [`_acp.py`](_acp.py) — `_select_allow_option` and
`TurnPolicyPermissionMixin` — because they are security-reviewed and reused rather than
reimplemented. `_select_allow_option` prefers the ACP option `kind` (`allow_once`, then
`allow_always`) and only falls back to a substring heuristic over the optionId, rejecting any
option bearing a negative marker so `disallow` can never be selected.

[`generic_acp.py`](generic_acp.py) is the client the non-policy-driving family subclasses
(`omp` / `opencode` / `kimi-code` / `mini-agent` / `dsh`). It carries the pieces that family
diverges on: the `_await_or_detect_death` liveness shape, no deny budget, no `stop_reason`, and a
`_respond_to_client_request` that cancels every permission ask (which the mixin overrides).
This class was called `GeminiAcpEngine` and lived in `gemini_acp.py` until 2026-08-29 — a
retired adapter's name on the live base of five shipping seats, which is how an omp seat came to
announce itself as "Gemini" and drain its child's stderr under a `[gemini-stderr]` prefix.
[`gemini_acp.py`](gemini_acp.py) is now a pure identity + command-line shim over it, and
`tests/test_generic_acp_shim.py` asserts it overrides no transport or turn-loop method.

Spawning goes through [`_stdio.py`](_stdio.py) exclusively: `scrubbed_child_env` +
`start_stderr_drain` are the only way a child process is created.

## Normalized events

Whatever the adapter, the daemon sees the same vocabulary:

```text
turn_started
turn_completed
turn_timeout
command_started
command_finished
model_text
```

## Adding an engine

Write the adapter, then satisfy the three checks that will otherwise fail — they are the actual
contract, so read them before designing around them:

1. **`tests/test_engine_support_tiers.py`.** Two assertions. `ENGINE_TO_TOOL` minus the pure
   aliases must equal the key set of `SUPPORT_TIERS` exactly, so a new engine has to be
   classified `certifying` / `experimental` / `retired`. And every `*.py` in this directory must
   be either a registered+tiered engine module (name-matched with `-` → `_`) or listed in the
   test's `_INFRA_MODULES` set with a reason in the commit. A new shared helper file lands in
   that set; a new adapter does not.
2. **`tests/test_dispatch_engine_parity.py`.** Every engine in `ENGINE_TO_TOOL` needs a case in
   `scripts/agent-dispatch`, mapping to the *same* tool prefix. A refusal branch counts (that is
   how `gemini-acp` stays legal), but silence does not.
3. **`tests/test_engine_spawn_env_ast_guard.py`.** An AST walk over every `subprocess.run` /
   `Popen` / `check_output` **call** in this directory. Each must pass an `env=` produced by
   `scrubbed_child_env`, `resolve_child_env`, or `scrub_env_dict` — a second, casual spawn (a
   `--version` preflight, say) inherits the daemon environment and is exactly what this guard
   caught. The only escape is an explicit `(path, function)` allowlist entry, which is a
   deliberate security decision with a stated reason, not a skip. The guard has a vacuity
   control: a synthetic un-scrubbed offender must be detected.

Then wire the branch in `bridge.build_engine()` and give the seat a supervised unit — see
[`../README.md`](../README.md) § "Systemd".

## See also

- [`../README.md`](../README.md) — the daemon, sender policy, safety knobs, dispatch recipe.
- [`../../../docs/bridge-parallelism.md`](../../../docs/bridge-parallelism.md) — the engine pool
  and `--max-parallel`; each extra slot spawns its own engine CLI process.
- [`../../../docs/agent-role-routing.md`](../../../docs/agent-role-routing.md) — which engine to
  dispatch for which job, and the evidence behind the default routing.
- [`../../../docs/implementor-routing.md`](../../../docs/implementor-routing.md) — implementor-side
  routing.
- [`../../../docs/upstream/pi-rpc-minimax-wedge.md`](../../../docs/upstream/pi-rpc-minimax-wedge.md)
  — an upstream-CLI failure written up against the adapter that hit it.

Cost note: Codex usage is tied to the configured ChatGPT/Codex plan. Historical Gemini ACP usage
was charged to the configured Gemini CLI account; `gemini-acp` is retired as of 2026-07-03 and
the entry points reject it, so that line survives only as policy/cost history. The grok-acp path
authenticates as whatever the local `grok` CLI is logged in as (TUI login or OAuth) — no
separate bridge-managed credentials — and maps the bridge's `trusted`/`human` sender policies to
Grok's ACP permission modes. Development of that path used a local, untracked `.env.grok-test`;
it is not checked in.
